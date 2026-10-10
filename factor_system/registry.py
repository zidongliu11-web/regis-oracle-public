"""因子注册中心（任务书第七部分·诚实适配 SQLite）。

生命周期状态机：
  candidate → validated → registered → backtesting → historical_pass / rejected
  historical_pass → production / rejected（仅在 2026+ 前向观察满 120 日后终测）

- candidate  : 挖掘出、通过 L0-L1（IC 显著）
- validated  : 通过 L0-L3（含 FDR 修正）
- registered : 已入库、等待回测
- backtesting: 回测执行中
- historical_pass : 历史确认通过，等待未参与搜索的前向观察
- production : 前向终测达标，正式投产
- rejected   : 任一环节淘汰（带原因）
- frozen     : 连续 STOP_FREEZE_DAYS 日 IC 转负 → 冻结停用

同一来源版本的重复候选通过
(family, formula, params_json, research_record_sha256) 去重，避免每轮重复入库；
不同来源版本永久保留为不同候选。
SQLite WAL 模式，单进程串行写入（8GB 单机）。
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pandas as pd

from . import config
from . import source_gate

SCHEMA = """
CREATE TABLE IF NOT EXISTS factors (
    factor_id      TEXT PRIMARY KEY,
    name           TEXT NOT NULL,
    family         TEXT NOT NULL,
    formula        TEXT NOT NULL,
    description    TEXT,
    direction      TEXT,
    data_requires  TEXT,
    kind           TEXT,
    params_json    TEXT,
    research_record_json TEXT,
    research_record_sha256 TEXT,
    status         TEXT NOT NULL DEFAULT 'candidate',
    is_ic_mean     REAL,
    is_ic_ir       REAL,
    oos_ic_mean    REAL,
    oos_ic_ir      REAL,
    ic_retention   REAL,
    long_short_annualized REAL,
    p_value        REAL,
    backtest_annual_return REAL,
    backtest_sharpe      REAL,
    backtest_max_drawdown REAL,
    backtest_win_rate    REAL,
    backtest_excess_annual REAL,
    backtest_benchmark_annual REAL,
    backtest_fold_pass_rate REAL,
    backtest_t_stat      REAL,
    backtest_evidence    TEXT,
    raw_ic_mean      REAL,
    raw_ic_ir        REAL,
    neutralize_mode  TEXT,
    monotonic_aligned REAL,
    ic_half_life     REAL,
    turnover_ratio   REAL,
    capacity_estimate REAL,
    incremental_ic   REAL,
    diag_json        TEXT,
    failure_json     TEXT,
    research_epoch   TEXT,
    validation_data_key TEXT,
    backtest_epoch   TEXT,
    oos_test_p_value REAL,
    backtest_gate_passed INTEGER,
    evidence_path    TEXT,
    evidence_sha256  TEXT,
    sealed_at        TEXT,
    sealed_data_end  TEXT,
    sealed_recipe_sha256 TEXT,
    evaluator_sealed_at TEXT,
    evaluator_sealed_data_end TEXT,
    evaluator_fingerprint TEXT,
    production_score_path TEXT,
    production_score_sha256 TEXT,
    production_data_key TEXT,
    production_as_of TEXT,
    production_evaluator_sha256 TEXT,
    production_admission_path TEXT,
    production_admission_sha256 TEXT,
    created_at     TEXT,
    updated_at     TEXT,
    status_reason  TEXT
);
CREATE INDEX IF NOT EXISTS idx_status ON factors(status);
CREATE INDEX IF NOT EXISTS idx_family ON factors(family);
CREATE TABLE IF NOT EXISTS factor_tests (
    test_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    epoch            TEXT NOT NULL,
    factor_id        TEXT NOT NULL,
    hypothesis_order INTEGER NOT NULL,
    p_value          REAL NOT NULL,
    alpha_i          REAL NOT NULL,
    gate_passed      INTEGER NOT NULL,
    online_reject    INTEGER NOT NULL,
    tested_at        TEXT NOT NULL,
    data_key         TEXT NOT NULL DEFAULT 'unspecified',
    p_value_method   TEXT NOT NULL DEFAULT 'non_overlapping_phase_max_hac',
    evaluator_fingerprint TEXT NOT NULL DEFAULT '',
    evidence_path    TEXT,
    evidence_sha256  TEXT,
    UNIQUE(epoch, factor_id),
    UNIQUE(epoch, hypothesis_order)
);
CREATE INDEX IF NOT EXISTS idx_factor_tests_epoch
    ON factor_tests(epoch, hypothesis_order);
CREATE TABLE IF NOT EXISTS test_epochs (
    epoch             TEXT PRIMARY KEY,
    q                 REAL NOT NULL,
    dependent         INTEGER NOT NULL,
    method            TEXT NOT NULL,
    method_version    TEXT NOT NULL,
    p_value_method    TEXT NOT NULL,
    initial_data_key  TEXT NOT NULL,
    evaluator_fingerprint TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS test_observation_latches (
    epoch             TEXT PRIMARY KEY,
    factor_id         TEXT NOT NULL,
    data_key          TEXT NOT NULL,
    p_value_method    TEXT NOT NULL,
    producer_type     TEXT NOT NULL DEFAULT 'unspecified',
    context_json      TEXT NOT NULL DEFAULT '{}',
    context_sha256    TEXT NOT NULL DEFAULT '',
    evaluator_fingerprint TEXT NOT NULL DEFAULT '',
    reserved_at       TEXT NOT NULL,
    UNIQUE(epoch, factor_id)
);
CREATE TABLE IF NOT EXISTS mining_batches (
    batch_id          TEXT PRIMARY KEY,
    research_epoch    TEXT NOT NULL,
    data_key          TEXT NOT NULL,
    family_json       TEXT NOT NULL,
    family_sha256     TEXT NOT NULL,
    context_json      TEXT NOT NULL DEFAULT '{}',
    progress_json     TEXT NOT NULL DEFAULT '{}',
    fdr_input_json    TEXT,
    fdr_input_sha256  TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
    created_at        TEXT NOT NULL,
    completed_at      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mining_pending_epoch
    ON mining_batches(research_epoch) WHERE status='pending';
CREATE TABLE IF NOT EXISTS candidate_blind_runs (
    authorization_sha256             TEXT PRIMARY KEY,
    authorization_path               TEXT NOT NULL,
    candidate_id                     TEXT NOT NULL UNIQUE,
    candidate_research_record_sha256 TEXT NOT NULL,
    implementation_identity_sha256   TEXT NOT NULL,
    implementation_audit_sha256      TEXT NOT NULL,
    evaluator_fingerprint            TEXT NOT NULL,
    panel_path                       TEXT NOT NULL,
    panel_key                        TEXT NOT NULL,
    panel_sha256                     TEXT NOT NULL,
    context_json                     TEXT NOT NULL,
    context_sha256                   TEXT NOT NULL,
    state                            TEXT NOT NULL,
    reserved_at                      TEXT NOT NULL,
    started_at                       TEXT,
    result_evidence_path             TEXT,
    result_evidence_sha256           TEXT,
    completed_at                     TEXT,
    terminal_reason                  TEXT,
    CHECK (state IN ('reserved','running','complete','consumed_unknown')),
    CHECK (state!='reserved' OR started_at IS NULL),
    CHECK (state!='running' OR started_at IS NOT NULL),
    CHECK (
        (state IN ('reserved','running')
         AND result_evidence_path IS NULL
         AND result_evidence_sha256 IS NULL
         AND completed_at IS NULL
         AND terminal_reason IS NULL)
        OR
        (state='complete'
         AND started_at IS NOT NULL
         AND result_evidence_path IS NOT NULL
         AND result_evidence_sha256 IS NOT NULL
         AND completed_at IS NOT NULL
         AND terminal_reason IS NULL)
        OR
        (state='consumed_unknown'
         AND result_evidence_path IS NULL
         AND result_evidence_sha256 IS NULL
         AND completed_at IS NOT NULL
         AND terminal_reason IS NOT NULL)
    )
);
CREATE TRIGGER IF NOT EXISTS candidate_blind_runs_identity_immutable
BEFORE UPDATE OF
    authorization_sha256, authorization_path, candidate_id,
    candidate_research_record_sha256, implementation_identity_sha256,
    implementation_audit_sha256, evaluator_fingerprint, panel_path,
    panel_key, panel_sha256, context_json, context_sha256, reserved_at
ON candidate_blind_runs
BEGIN
    SELECT RAISE(ABORT, 'candidate_blind_runs identity is immutable');
END;
CREATE TRIGGER IF NOT EXISTS candidate_blind_runs_terminal_immutable
BEFORE UPDATE ON candidate_blind_runs
WHEN OLD.state IN ('complete','consumed_unknown')
BEGIN
    SELECT RAISE(ABORT, 'candidate_blind_runs terminal state is immutable');
END;
CREATE TRIGGER IF NOT EXISTS candidate_blind_runs_transition_guard
BEFORE UPDATE OF state ON candidate_blind_runs
WHEN NOT (
    (OLD.state='reserved' AND NEW.state IN ('running','consumed_unknown'))
    OR (OLD.state='running' AND NEW.state IN ('complete','consumed_unknown'))
)
BEGIN
    SELECT RAISE(ABORT, 'candidate_blind_runs state transition invalid');
END;
CREATE TRIGGER IF NOT EXISTS candidate_blind_runs_no_delete
BEFORE DELETE ON candidate_blind_runs
BEGIN
    SELECT RAISE(ABORT, 'candidate_blind_runs is append-only');
END;
CREATE TABLE IF NOT EXISTS blind_post_admissions (
    admission_sha256                 TEXT PRIMARY KEY,
    admission_path                   TEXT NOT NULL UNIQUE,
    candidate_id                     TEXT NOT NULL UNIQUE,
    authorization_sha256             TEXT NOT NULL UNIQUE,
    result_evidence_path             TEXT NOT NULL,
    result_evidence_sha256           TEXT NOT NULL,
    candidate_research_record_sha256 TEXT NOT NULL,
    sealed_recipe_sha256             TEXT NOT NULL,
    evaluator_fingerprint            TEXT NOT NULL,
    panel_sha256                     TEXT NOT NULL,
    artifact_json                    TEXT NOT NULL,
    admitted_at                      TEXT NOT NULL,
    FOREIGN KEY (authorization_sha256)
        REFERENCES candidate_blind_runs(authorization_sha256)
);
CREATE TRIGGER IF NOT EXISTS blind_post_admissions_no_update
BEFORE UPDATE ON blind_post_admissions
BEGIN
    SELECT RAISE(ABORT, 'blind_post_admissions is append-only');
END;
CREATE TRIGGER IF NOT EXISTS blind_post_admissions_no_delete
BEFORE DELETE ON blind_post_admissions
BEGIN
    SELECT RAISE(ABORT, 'blind_post_admissions is append-only');
END;
CREATE TRIGGER IF NOT EXISTS blind_post_admissions_insert_guard
BEFORE INSERT ON blind_post_admissions
WHEN NEW.admission_path != (
        'factor_system/registry/blind_post_admissions/'
        || NEW.admission_sha256 || '.json'
     )
  OR json_valid(NEW.artifact_json)!=1
  OR json_extract(NEW.artifact_json, '$.candidate_id') IS NOT NEW.candidate_id
  OR json_extract(NEW.artifact_json, '$.bindings.authorization.sha256')
       IS NOT NEW.authorization_sha256
  OR json_extract(NEW.artifact_json, '$.bindings.result_evidence.path')
       IS NOT NEW.result_evidence_path
  OR json_extract(NEW.artifact_json, '$.bindings.result_evidence.sha256')
       IS NOT NEW.result_evidence_sha256
  OR json_extract(NEW.artifact_json, '$.bindings.research_record_sha256')
       IS NOT NEW.candidate_research_record_sha256
  OR json_extract(NEW.artifact_json, '$.bindings.sealed_recipe_sha256')
       IS NOT NEW.sealed_recipe_sha256
  OR json_extract(NEW.artifact_json, '$.bindings.evaluator_fingerprint')
       IS NOT NEW.evaluator_fingerprint
  OR json_extract(NEW.artifact_json, '$.decision.blind_final_status')
       IS NOT 'validated_locked'
  OR json_extract(NEW.artifact_json, '$.decision.l3_passed') IS NOT 1
  OR json_extract(NEW.artifact_json, '$.decision.fdr_reject') IS NOT 1
  OR json_extract(NEW.artifact_json, '$.decision.production_unlocked') IS NOT 0
  OR NOT EXISTS (
    SELECT 1
      FROM candidate_blind_runs AS run
      JOIN factors AS factor ON factor.factor_id=run.candidate_id
     WHERE run.candidate_id=NEW.candidate_id
       AND run.authorization_sha256=NEW.authorization_sha256
       AND run.state='complete'
       AND run.result_evidence_path=NEW.result_evidence_path
       AND run.result_evidence_sha256=NEW.result_evidence_sha256
       AND run.candidate_research_record_sha256=
           NEW.candidate_research_record_sha256
       AND run.evaluator_fingerprint=NEW.evaluator_fingerprint
       AND run.panel_sha256=NEW.panel_sha256
       AND factor.status='validated_locked'
       AND factor.research_record_sha256=
           NEW.candidate_research_record_sha256
       AND factor.sealed_recipe_sha256=NEW.sealed_recipe_sha256
  )
BEGIN
    SELECT RAISE(ABORT, 'blind post-admission requires exact validated evidence');
END;
CREATE TRIGGER IF NOT EXISTS candidate_blind_runs_factor_status_guard
BEFORE INSERT ON candidate_blind_runs
WHEN EXISTS (
    SELECT 1 FROM factors
    WHERE factor_id=NEW.candidate_id AND status!='candidate'
)
BEGIN
    SELECT RAISE(ABORT, 'blind run requires candidate factor status');
END;
CREATE TRIGGER IF NOT EXISTS candidate_blind_runs_insert_state_guard
BEFORE INSERT ON candidate_blind_runs
WHEN NEW.state!='reserved'
  OR NEW.started_at IS NOT NULL
  OR NEW.result_evidence_path IS NOT NULL
  OR NEW.result_evidence_sha256 IS NOT NULL
  OR NEW.completed_at IS NOT NULL
  OR NEW.terminal_reason IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'blind run must be inserted as an empty reservation');
END;
CREATE TRIGGER IF NOT EXISTS blind_factor_insert_guard
BEFORE INSERT ON factors
WHEN EXISTS (
    SELECT 1 FROM candidate_blind_runs
    WHERE candidate_id=NEW.factor_id
) AND NEW.status!='candidate'
BEGIN
    SELECT RAISE(ABORT, 'blind factor must start as candidate');
END;
CREATE TRIGGER IF NOT EXISTS blind_factor_status_guard
BEFORE UPDATE OF status ON factors
WHEN EXISTS (
    SELECT 1 FROM candidate_blind_runs
    WHERE candidate_id=NEW.factor_id
) AND NOT (
    (
        NOT EXISTS (
            SELECT 1 FROM blind_post_admissions
             WHERE candidate_id=NEW.factor_id
               AND candidate_research_record_sha256=NEW.research_record_sha256
               AND sealed_recipe_sha256=NEW.sealed_recipe_sha256
        )
        AND (
            (OLD.status=NEW.status AND NEW.status IN (
                'candidate','validated_locked','rejected','blind_unknown_locked'
            ))
            OR (OLD.status='candidate' AND NEW.status IN (
                'validated_locked','rejected','blind_unknown_locked'
            ))
        )
    )
    OR (
        EXISTS (
            SELECT 1 FROM blind_post_admissions
             WHERE candidate_id=NEW.factor_id
               AND candidate_research_record_sha256=NEW.research_record_sha256
               AND sealed_recipe_sha256=NEW.sealed_recipe_sha256
        )
        AND (
            (OLD.status='validated_locked' AND NEW.status='registered')
            OR (OLD.status='registered' AND NEW.status IN (
                'registered','backtesting','rejected','invalidated'
            ))
            OR (OLD.status='backtesting' AND NEW.status IN (
                'registered','backtesting','historical_pass','rejected',
                'invalidated','production'
            ))
            OR (OLD.status='historical_pass' AND NEW.status IN (
                'historical_pass','backtesting','rejected','invalidated','frozen'
            ))
            OR (OLD.status='production' AND NEW.status IN (
                'production','frozen','rejected','invalidated'
            ))
            OR (OLD.status=NEW.status AND NEW.status IN (
                'rejected','invalidated','frozen'
            ))
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'blind factor status transition is locked');
END;
CREATE TRIGGER IF NOT EXISTS blind_factor_no_delete
BEFORE DELETE ON factors
WHEN EXISTS (
    SELECT 1 FROM candidate_blind_runs
    WHERE candidate_id=OLD.factor_id
)
BEGIN
    SELECT RAISE(ABORT, 'blind factor is append-only');
END;
CREATE TABLE IF NOT EXISTS independent_verifier_commitments (
    candidate_id                     TEXT PRIMARY KEY,
    commitment_sha256                TEXT NOT NULL UNIQUE,
    commitment_path                  TEXT NOT NULL UNIQUE,
    verifier_path                    TEXT NOT NULL UNIQUE,
    verifier_sha256                  TEXT NOT NULL,
    candidate_research_record_sha256 TEXT NOT NULL,
    sealed_recipe_sha256             TEXT NOT NULL,
    evaluator_fingerprint            TEXT NOT NULL,
    artifact_json                    TEXT NOT NULL,
    committed_at                     TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS independent_verifier_commitments_no_update
BEFORE UPDATE ON independent_verifier_commitments
BEGIN
    SELECT RAISE(ABORT, 'independent verifier commitment is append-only');
END;
CREATE TRIGGER IF NOT EXISTS independent_verifier_commitments_no_delete
BEFORE DELETE ON independent_verifier_commitments
BEGIN
    SELECT RAISE(ABORT, 'independent verifier commitment is append-only');
END;
CREATE TRIGGER IF NOT EXISTS independent_verifier_commitments_insert_guard
BEFORE INSERT ON independent_verifier_commitments
WHEN NOT EXISTS (
    SELECT 1 FROM factors AS factor
     WHERE factor.factor_id=NEW.candidate_id
       AND factor.status IN ('registered','backtesting','historical_pass')
       AND factor.research_record_sha256=
           NEW.candidate_research_record_sha256
       AND factor.sealed_recipe_sha256=NEW.sealed_recipe_sha256
       AND factor.evaluator_fingerprint=NEW.evaluator_fingerprint
       AND NOT EXISTS (
           SELECT 1 FROM factor_tests AS test
            WHERE test.factor_id=NEW.candidate_id
              AND test.epoch LIKE '%:forward-shadow-sealed-v2'
       )
)
BEGIN
    SELECT RAISE(ABORT, 'independent verifier must be committed before forward observation');
END;
CREATE TABLE IF NOT EXISTS production_catalog (
    catalog_id       INTEGER PRIMARY KEY CHECK (catalog_id = 1),
    research_epoch   TEXT NOT NULL,
    catalog_version  TEXT NOT NULL,
    manifest_json    TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS factor_trial_ledger (
    snapshot_sha256       TEXT PRIMARY KEY,
    factor_id             TEXT NOT NULL,
    research_record_sha256 TEXT,
    research_epoch        TEXT,
    factor_status         TEXT NOT NULL,
    event                 TEXT NOT NULL,
    snapshot_json         TEXT NOT NULL,
    recorded_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_factor_trial_ledger_factor
    ON factor_trial_ledger(factor_id, recorded_at);
CREATE TRIGGER IF NOT EXISTS factor_trial_ledger_no_update
BEFORE UPDATE ON factor_trial_ledger
BEGIN
    SELECT RAISE(ABORT, 'factor_trial_ledger is append-only');
END;
CREATE TRIGGER IF NOT EXISTS factor_trial_ledger_no_delete
BEFORE DELETE ON factor_trial_ledger
BEGIN
    SELECT RAISE(ABORT, 'factor_trial_ledger is append-only');
END;
"""


_MIGRATIONS = [
    # 最早期 registry 只有身份与状态列；先补齐后续清理语句会引用的基础列。
    ("description", "TEXT"),
    ("direction", "TEXT"),
    ("data_requires", "TEXT"),
    ("kind", "TEXT"),
    ("is_ic_mean", "REAL"),
    ("is_ic_ir", "REAL"),
    ("oos_ic_mean", "REAL"),
    ("oos_ic_ir", "REAL"),
    ("ic_retention", "REAL"),
    ("long_short_annualized", "REAL"),
    ("p_value", "REAL"),
    ("backtest_annual_return", "REAL"),
    ("backtest_sharpe", "REAL"),
    ("backtest_max_drawdown", "REAL"),
    ("backtest_win_rate", "REAL"),
    ("backtest_excess_annual", "REAL"),
    ("backtest_benchmark_annual", "REAL"),
    ("created_at", "TEXT"),
    ("updated_at", "TEXT"),
    ("status_reason", "TEXT"),
    # P2-4 科学闸门证据列（老库 ALTER 迁移；新库 SCHEMA 已含）
    ("backtest_fold_pass_rate", "REAL"),
    ("backtest_t_stat", "REAL"),
    ("backtest_evidence", "TEXT"),
    # M1.5 双 IC / 中性化 / 专业诊断证据列（soft/报告层 KPI + 全量 diag JSON）
    ("raw_ic_mean", "REAL"),
    ("raw_ic_ir", "REAL"),
    ("neutralize_mode", "TEXT"),
    ("monotonic_aligned", "REAL"),
    ("ic_half_life", "REAL"),
    ("turnover_ratio", "REAL"),
    ("capacity_estimate", "REAL"),
    ("incremental_ic", "REAL"),
    ("diag_json", "TEXT"),
    ("failure_json", "TEXT"),
    # strict-3way epoch + 历史确认的在线多重检验/证据账本
    ("research_epoch", "TEXT"),
    ("validation_data_key", "TEXT"),
    ("backtest_epoch", "TEXT"),
    ("oos_test_p_value", "REAL"),
    ("backtest_gate_passed", "INTEGER"),
    ("evidence_path", "TEXT"),
    ("evidence_sha256", "TEXT"),
    # 每个因子的不可变假设冻结点。前向终测只能消费 sealed_data_end 之后
    # 才到达的数据，避免后生成的因子复用早已存在的“前向”行情。
    ("sealed_at", "TEXT"),
    ("sealed_data_end", "TEXT"),
    ("sealed_recipe_sha256", "TEXT"),
    ("evaluator_sealed_at", "TEXT"),
    ("evaluator_sealed_data_end", "TEXT"),
    ("evaluator_fingerprint", "TEXT"),
    # production 状态与可消费的、内容寻址全市场分数制品必须同事务落库。
    ("production_score_path", "TEXT"),
    ("production_score_sha256", "TEXT"),
    ("production_data_key", "TEXT"),
    ("production_as_of", "TEXT"),
    ("production_evaluator_sha256", "TEXT"),
    ("production_admission_path", "TEXT"),
    ("production_admission_sha256", "TEXT"),
    # 来源与预注册记录跟随候选身份进入不可变链。
    ("research_record_json", "TEXT"),
    ("research_record_sha256", "TEXT"),
]

_TEST_MIGRATIONS = [
    ("data_key", "TEXT NOT NULL DEFAULT 'unspecified'"),
    ("p_value_method", "TEXT NOT NULL DEFAULT 'non_overlapping_phase_max_hac'"),
    ("evaluator_fingerprint", "TEXT NOT NULL DEFAULT ''"),
]

_TEST_EPOCH_MIGRATIONS = [
    ("evaluator_fingerprint", "TEXT NOT NULL DEFAULT ''"),
]

_MINING_BATCH_MIGRATIONS = [
    ("context_json", "TEXT NOT NULL DEFAULT '{}'"),
]

_OBSERVATION_LATCH_MIGRATIONS = [
    ("producer_type", "TEXT NOT NULL DEFAULT 'unspecified'"),
    ("context_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("context_sha256", "TEXT NOT NULL DEFAULT ''"),
    ("evaluator_fingerprint", "TEXT NOT NULL DEFAULT ''"),
]


_RESULT_EVIDENCE_COLUMNS = (
    "is_ic_mean", "is_ic_ir", "oos_ic_mean", "oos_ic_ir", "ic_retention",
    "long_short_annualized", "p_value", "backtest_annual_return",
    "backtest_sharpe", "backtest_max_drawdown", "backtest_win_rate",
    "backtest_excess_annual", "backtest_benchmark_annual",
    "backtest_fold_pass_rate", "backtest_t_stat", "backtest_evidence",
    "raw_ic_mean", "raw_ic_ir", "neutralize_mode", "monotonic_aligned",
    "ic_half_life", "turnover_ratio", "capacity_estimate", "incremental_ic",
    "diag_json", "backtest_epoch", "oos_test_p_value",
    "backtest_gate_passed", "evidence_path", "evidence_sha256", "sealed_at",
    "sealed_data_end", "sealed_recipe_sha256", "evaluator_sealed_at",
    "evaluator_sealed_data_end", "evaluator_fingerprint",
    "production_score_path", "production_score_sha256", "production_data_key",
    "production_as_of", "production_evaluator_sha256",
    "production_admission_path", "production_admission_sha256",
)

_TERMINAL_FACTOR_STATUSES = frozenset({"rejected", "invalidated", "frozen"})


def _null_assignments(columns: tuple[str, ...]) -> str:
    return ",".join(f"{column}=NULL" for column in columns)


def _clear_result_evidence(c: sqlite3.Connection, fid: str) -> None:
    c.execute(
        f"UPDATE factors SET {_null_assignments(_RESULT_EVIDENCE_COLUMNS)} "
        "WHERE factor_id=?",
        (fid,),
    )


def _snapshot_value(value):
    if isinstance(value, dict):
        return {str(key): _snapshot_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_snapshot_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _query_dicts(c: sqlite3.Connection, sql: str, params: tuple) -> list[dict]:
    cursor = c.execute(sql, params)
    columns = [item[0] for item in cursor.description]
    return [
        dict(row) if isinstance(row, sqlite3.Row) else dict(zip(columns, row))
        for row in cursor.fetchall()
    ]


def _append_factor_trial_snapshot(c: sqlite3.Connection, fid: str, *,
                                  event: str, details: dict | None = None
                                  ) -> str | None:
    """Append one content-addressed full factor snapshot; never mutate it."""
    factors = _query_dicts(
        c, "SELECT * FROM factors WHERE factor_id=?", (fid,),
    )
    if not factors:
        return None
    try:
        tests = _query_dicts(
            c, "SELECT * FROM factor_tests WHERE factor_id=? ORDER BY test_id",
            (fid,),
        )
    except sqlite3.OperationalError as exc:
        if "no such table: factor_tests" not in str(exc):
            raise
        tests = []
    factor = factors[0]
    payload = _snapshot_value({
        "schema_version": 1,
        "event": event,
        "factor": factor,
        "factor_tests": tests,
        "details": details or {},
    })
    body = _canonical_json(payload)
    digest = _json_sha256(body)
    try:
        c.execute(
            """INSERT OR IGNORE INTO factor_trial_ledger
                 (snapshot_sha256,factor_id,research_record_sha256,
                  research_epoch,factor_status,event,snapshot_json,recorded_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                digest, fid, factor.get("research_record_sha256"),
                factor.get("research_epoch"), str(factor.get("status") or ""),
                event, body, _now(),
            ),
        )
    except sqlite3.OperationalError as exc:
        # Narrow compatibility schemas are used by recovery tools/tests only.
        if "no such table: factor_trial_ledger" not in str(exc):
            raise
        return None
    return digest


def _reset_for_research_epoch(c: sqlite3.Connection, fid: str, now: str) -> None:
    """Reuse formula identity without carrying a previous epoch's conclusion."""
    _append_factor_trial_snapshot(
        c, fid, event="pre_epoch_reset",
        details={"next_research_epoch": config.RESEARCH_EPOCH},
    )
    c.execute(
        f"UPDATE factors SET status='candidate',status_reason='',"
        f"{_null_assignments(_RESULT_EVIDENCE_COLUMNS)},"
        "failure_json=NULL,research_epoch=NULL,validation_data_key=NULL,"
        "created_at=?,updated_at=? "
        "WHERE factor_id=?",
        (now, now, fid),
    )


def _migrate(c: sqlite3.Connection):
    for col, ctype in _MIGRATIONS:
        try:
            c.execute(f"ALTER TABLE factors ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass  # 列已存在
    for col, ctype in _TEST_MIGRATIONS:
        try:
            c.execute(f"ALTER TABLE factor_tests ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass
    for col, ctype in _TEST_EPOCH_MIGRATIONS:
        try:
            c.execute(f"ALTER TABLE test_epochs ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass
    for col, ctype in _MINING_BATCH_MIGRATIONS:
        try:
            c.execute(f"ALTER TABLE mining_batches ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass
    for col, ctype in _OBSERVATION_LATCH_MIGRATIONS:
        try:
            c.execute(f"ALTER TABLE test_observation_latches ADD COLUMN {col} {ctype}")
        except sqlite3.OperationalError:
            pass
    empty_context_sha = hashlib.sha256(b"{}").hexdigest()
    c.execute(
        "UPDATE test_observation_latches SET context_sha256=? "
        "WHERE context_sha256=''",
        (empty_context_sha,),
    )
    # Early-stage terminal rows have no current validation/backtest evidence.
    # Old partial upserts used to leave a previous epoch's metrics attached.
    c.execute(
        f"UPDATE factors SET {_null_assignments(_RESULT_EVIDENCE_COLUMNS)} "
        "WHERE status='rejected' AND (status_reason LIKE 'compute:%' "
        "OR status_reason LIKE 'L0:%' OR status_reason LIKE 'L1:%' "
        "OR status_reason LIKE '新颖性归档:%')"
    )
    for row in c.execute(
        "SELECT factor_id,status_reason FROM factors "
        "WHERE status='rejected' AND failure_json IS NULL"
    ).fetchall():
        fid = row["factor_id"] if isinstance(row, sqlite3.Row) else row[0]
        reason = row["status_reason"] if isinstance(row, sqlite3.Row) else row[1]
        failure = _failure_diagnostic(reason=reason or "")
        c.execute(
            "UPDATE factors SET failure_json=? WHERE factor_id=?",
            (_canonical_json(failure), fid),
        )
    # The former three-column key collapsed distinct preregistrations. Preserve
    # legacy NULL/blank provenance exactly as stored: the expression gives each
    # unverifiable legacy row a factor-scoped index identity without writing a
    # fake digest into the research_record_sha256 column. Verified hashes remain
    # strictly unique for the complete scientific-trial key.
    c.execute("DROP INDEX IF EXISTS uq_factor_formula_params")
    c.execute("DROP INDEX IF EXISTS uq_factor_formula_params_source")
    c.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS uq_factor_formula_params_source
             ON factors(
                 family,
                 formula,
                 params_json,
                 COALESCE(
                     NULLIF(TRIM(research_record_sha256), ''),
                     'legacy:' || factor_id
                 )
             )"""
    )
    # Existing databases already own the older fail-closed triggers. Recreate
    # them so post-blind admission unlocks only the normal lifecycle, while the
    # admission row itself remains tied to its canonical artifact body.
    c.executescript(
        """
        DROP TRIGGER IF EXISTS blind_factor_status_guard;
        CREATE TRIGGER blind_factor_status_guard
        BEFORE UPDATE OF status ON factors
        WHEN EXISTS (
            SELECT 1 FROM candidate_blind_runs
            WHERE candidate_id=NEW.factor_id
        ) AND NOT (
            (
                NOT EXISTS (
                    SELECT 1 FROM blind_post_admissions
                     WHERE candidate_id=NEW.factor_id
                       AND candidate_research_record_sha256=NEW.research_record_sha256
                       AND sealed_recipe_sha256=NEW.sealed_recipe_sha256
                )
                AND (
                    (OLD.status=NEW.status AND NEW.status IN (
                        'candidate','validated_locked','rejected','blind_unknown_locked'
                    ))
                    OR (OLD.status='candidate' AND NEW.status IN (
                        'validated_locked','rejected','blind_unknown_locked'
                    ))
                )
            )
            OR (
                EXISTS (
                    SELECT 1 FROM blind_post_admissions
                     WHERE candidate_id=NEW.factor_id
                       AND candidate_research_record_sha256=NEW.research_record_sha256
                       AND sealed_recipe_sha256=NEW.sealed_recipe_sha256
                )
                AND (
                    (OLD.status='validated_locked' AND NEW.status='registered')
                    OR (OLD.status='registered' AND NEW.status IN (
                        'registered','backtesting','rejected','invalidated'
                    ))
                    OR (OLD.status='backtesting' AND NEW.status IN (
                        'registered','backtesting','historical_pass','rejected',
                        'invalidated','production'
                    ))
                    OR (OLD.status='historical_pass' AND NEW.status IN (
                        'historical_pass','backtesting','rejected','invalidated','frozen'
                    ))
                    OR (OLD.status='production' AND NEW.status IN (
                        'production','frozen','rejected','invalidated'
                    ))
                    OR (OLD.status=NEW.status AND NEW.status IN (
                        'rejected','invalidated','frozen'
                    ))
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'blind factor status transition is locked');
        END;

        DROP TRIGGER IF EXISTS blind_post_admissions_insert_guard;
        CREATE TRIGGER blind_post_admissions_insert_guard
        BEFORE INSERT ON blind_post_admissions
        WHEN NEW.admission_path != (
                'factor_system/registry/blind_post_admissions/'
                || NEW.admission_sha256 || '.json'
             )
          OR json_valid(NEW.artifact_json)!=1
          OR json_extract(NEW.artifact_json, '$.candidate_id') IS NOT NEW.candidate_id
          OR json_extract(NEW.artifact_json, '$.bindings.authorization.sha256')
               IS NOT NEW.authorization_sha256
          OR json_extract(NEW.artifact_json, '$.bindings.result_evidence.path')
               IS NOT NEW.result_evidence_path
          OR json_extract(NEW.artifact_json, '$.bindings.result_evidence.sha256')
               IS NOT NEW.result_evidence_sha256
          OR json_extract(NEW.artifact_json, '$.bindings.research_record_sha256')
               IS NOT NEW.candidate_research_record_sha256
          OR json_extract(NEW.artifact_json, '$.bindings.sealed_recipe_sha256')
               IS NOT NEW.sealed_recipe_sha256
          OR json_extract(NEW.artifact_json, '$.bindings.evaluator_fingerprint')
               IS NOT NEW.evaluator_fingerprint
          OR json_extract(NEW.artifact_json, '$.decision.blind_final_status')
               IS NOT 'validated_locked'
          OR json_extract(NEW.artifact_json, '$.decision.l3_passed') IS NOT 1
          OR json_extract(NEW.artifact_json, '$.decision.fdr_reject') IS NOT 1
          OR json_extract(NEW.artifact_json, '$.decision.production_unlocked') IS NOT 0
          OR NOT EXISTS (
            SELECT 1
              FROM candidate_blind_runs AS run
              JOIN factors AS factor ON factor.factor_id=run.candidate_id
             WHERE run.candidate_id=NEW.candidate_id
               AND run.authorization_sha256=NEW.authorization_sha256
               AND run.state='complete'
               AND run.result_evidence_path=NEW.result_evidence_path
               AND run.result_evidence_sha256=NEW.result_evidence_sha256
               AND run.candidate_research_record_sha256=
                   NEW.candidate_research_record_sha256
               AND run.evaluator_fingerprint=NEW.evaluator_fingerprint
               AND run.panel_sha256=NEW.panel_sha256
               AND factor.status='validated_locked'
               AND factor.research_record_sha256=
                   NEW.candidate_research_record_sha256
               AND factor.sealed_recipe_sha256=NEW.sealed_recipe_sha256
          )
        BEGIN
            SELECT RAISE(ABORT, 'blind post-admission requires exact validated evidence');
        END;
        """
    )
    terminal_ids = c.execute(
        "SELECT factor_id FROM factors WHERE status IN ('rejected','invalidated','frozen')"
    ).fetchall()
    for row in terminal_ids:
        fid = row["factor_id"] if isinstance(row, sqlite3.Row) else row[0]
        _append_factor_trial_snapshot(
            c, fid, event="migration_terminal_backfill",
        )


_MIGRATED = False  # 进程内幂等标记：DDL 每进程只跑一次（M2.1，杜绝批处理期反复 DDL）


def migrate_schema(c: sqlite3.Connection | None = None):
    """建表 + ALTER 迁移 + WAL 设置，进程内幂等。

    M2.1：DDL 从 _conn() 移出。supervisor / 5200 启动各显式调用一次；
    _conn() 首次惰性兜底。批处理运行中绝不重跑 DDL。
    """
    global _MIGRATED
    if _MIGRATED:
        return
    own = c is None
    if own:
        config.REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
    cfg = c if c is not None else sqlite3.connect(str(config.REGISTRY_DIR / "registry.db"))
    cfg.execute("PRAGMA journal_mode=WAL")
    cfg.execute("PRAGMA synchronous=NORMAL")
    cfg.executescript(SCHEMA)
    _migrate(cfg)
    cfg.commit()
    if own:
        cfg.close()
    _MIGRATED = True


def _conn(readonly: bool = False, migrate: bool = True) -> sqlite3.Connection:
    """打开连接。readonly=True 用 mode=ro（看板端点，绝不写/不 DDL）。"""
    db = config.REGISTRY_DIR / "registry.db"
    if readonly:
        if not db.exists():
            raise FileNotFoundError("factor_registry_missing")
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    else:
        config.REGISTRY_DIR.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(str(db))
    # M2.1：60s busy_timeout，串行批处理与看板并发时不再立即 SQLITE_BUSY
    c.execute("PRAGMA busy_timeout=60000")
    c.row_factory = sqlite3.Row
    if migrate and not readonly:
        migrate_schema(c)   # 进程内幂等
    return c


def _now() -> str:
    # 避免 Date.now 类依赖，用本机时间
    import datetime
    return datetime.datetime.now().isoformat(timespec="seconds")


def _upsert(c: sqlite3.Connection, row: dict):
    keys = list(row.keys())
    cols = ", ".join(keys)
    ph = ", ".join("?" for _ in keys)
    upd = ", ".join(f"{k}=excluded.{k}" for k in keys if k != "factor_id")
    sql = f"INSERT INTO factors ({cols}) VALUES ({ph}) ON CONFLICT(factor_id) DO UPDATE SET {upd}"
    c.execute(sql, [row[k] for k in keys])


def _candidate_source_context(cand) -> dict:
    record = getattr(cand, "research_record", None)
    digest = str(getattr(cand, "research_record_sha256", "") or "")
    candidate = record.get("candidate") if isinstance(record, dict) else None
    candidate_id = candidate.get("candidate_id") if isinstance(candidate, dict) else None
    result = source_gate.verify_candidate(
        str(candidate_id or ""), source_gate.load_catalog(config.SOURCE_CATALOG),
    )
    local_spec = (
        ((result.get("research_record") or {}).get("candidate") or {})
        .get("formula", {}).get("local_implementation")
    )
    expected_spec = {
        "factor_id": str(getattr(cand, "factor_id", "")),
        "family": str(getattr(cand, "family", "")),
        "formula": str(getattr(cand, "formula", "")),
        "params": (
            getattr(cand, "params", {})
            if isinstance(getattr(cand, "params", {}), dict) else {}
        ),
        "data_requires": list(getattr(cand, "data_requires", ())),
        "direction": str(getattr(cand, "direction", "")),
    }
    identity_ok = result.get("candidate_id") == str(
        getattr(cand, "factor_id", "")
    )
    return {
        "record": record,
        "digest": digest,
        "result": result,
        "local_spec": local_spec,
        "expected_spec": expected_spec,
        "identity_ok": identity_ok,
    }


def verify_candidate_source(cand) -> dict:
    """根注册表重新校验普通 runnable Candidate 的来源冻结记录。"""
    source = _candidate_source_context(cand)
    result = source["result"]
    if (not source["identity_ok"]
            or source["local_spec"] != source["expected_spec"]
            or not result["valid"] or not result["runnable"]
            or result["research_record"] != source["record"]
            or result["research_record_sha256"] != source["digest"]):
        detail = ",".join(result["errors"] or [
            "candidate_identity_mismatch" if not source["identity_ok"]
            else "local_implementation_mismatch"
            if source["local_spec"] != source["expected_spec"]
            else "candidate_not_runnable"
        ])
        raise ValueError(f"[source_record_invalid] {detail}")
    return result


def verify_blind_candidate_source(
    cand, authorization_sha256: str, c: sqlite3.Connection,
) -> dict:
    """Verify a built, non-runnable candidate against its active blind claim."""
    authorization_sha256 = _blind_run_hash(
        authorization_sha256, "authorization_sha256",
    )
    claim = _blind_run_for_authorization(c, authorization_sha256)
    if claim is None:
        raise ValueError("[blind_authorization_missing] blind run 未预约")
    if claim["state"] not in _BLIND_RUN_ACTIVE_STATES:
        raise ValueError(
            f"[blind_authorization_terminal] blind run 已终态: {claim['state']}"
        )
    source = _candidate_source_context(cand)
    result = source["result"]
    factor_id = str(getattr(cand, "factor_id", ""))
    if (claim["candidate_id"] != factor_id
            or claim["candidate_research_record_sha256"] != source["digest"]):
        raise ValueError(
            "[blind_authorization_identity_mismatch] "
            "candidate 或 research record 与 blind claim 不一致"
        )
    valid_built_source = (
        source["identity_ok"]
        and source["local_spec"] == source["expected_spec"]
        and result.get("valid") is True
        and result.get("status") == "implementation_built"
        and result.get("runnable") is False
        and result.get("research_record") == source["record"]
        and result.get("research_record_sha256") == source["digest"]
    )
    if not valid_built_source:
        detail = ",".join(result.get("errors") or [
            "candidate_identity_mismatch" if not source["identity_ok"]
            else "local_implementation_mismatch"
            if source["local_spec"] != source["expected_spec"]
            else "candidate_not_built_locked",
        ])
        raise ValueError(f"[blind_source_record_invalid] {detail}")
    return result


def _frozen_source_result(row, recipe: dict) -> dict:
    """Normalize a frozen SQLite/recipe pair and run the canonical gate."""
    stored = dict(row)
    record = stored.get("research_record")
    if not isinstance(record, dict):
        try:
            record = json.loads(stored.get("research_record_json") or "")
        except (TypeError, json.JSONDecodeError):
            record = None
    candidate = record.get("candidate") if isinstance(record, dict) else None
    source_status = candidate.get("status") if isinstance(candidate, dict) else None
    normalized = {
        **stored,
        "source_candidate_id": (
            candidate.get("candidate_id") if isinstance(candidate, dict) else None
        ),
        "status": source_status,
        "runnable": (
            candidate.get("runnable") if isinstance(candidate, dict) else None
        ),
        "research_record": record,
    }
    frozen_recipe = {
        **recipe,
        "source_candidate_id": normalized["source_candidate_id"],
        "status": source_status,
        "runnable": normalized["runnable"],
    }
    result = source_gate.verify_frozen_record(
        normalized, frozen_recipe,
        source_gate.load_catalog(config.SOURCE_CATALOG),
    )
    return result


def verify_frozen_source_record(row, recipe: dict) -> dict:
    """Verify an ordinary runnable source record."""
    result = _frozen_source_result(row, recipe)
    if not result["valid"] or not result["runnable"]:
        detail = ",".join(result["errors"] or ["candidate_not_runnable"])
        raise ValueError(f"[source_record_invalid] {detail}")
    return result


def verify_blind_built_source_record(row, recipe: dict) -> dict:
    """Verify the exact built/non-runnable record frozen before blind outcome."""
    result = _frozen_source_result(row, recipe)
    if (result.get("valid") is not True
            or result.get("status") != "implementation_built"
            or result.get("runnable") is not False):
        detail = ",".join(result.get("errors") or [
            "candidate_not_built_locked",
        ])
        raise ValueError(f"[blind_source_record_invalid] {detail}")
    return result


def _register_candidate_verified(c: sqlite3.Connection, cand) -> str:
    """Insert a candidate only after the caller selected the strict verifier."""
    research_record = getattr(cand, "research_record", None)
    research_sha256 = str(getattr(cand, "research_record_sha256", "") or "")
    if not isinstance(research_record, dict) or not research_record or not research_sha256:
        raise ValueError("[source_record_missing] 候选缺少已验证的来源与预注册记录")
    research_json = json.dumps(
        research_record, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )
    if isinstance(cand.params, dict):
        params_json = json.dumps(cand.params, ensure_ascii=False, sort_keys=True)
    else:
        params_json = "{}"

    def frozen_identity():
        by_id = c.execute(
            """SELECT factor_id,family,formula,params_json,research_epoch,
                      research_record_sha256
                 FROM factors WHERE factor_id=?""",
            (cand.factor_id,),
        ).fetchone()
        if by_id is not None:
            if by_id["research_record_sha256"] != research_sha256:
                raise ValueError(
                    "[source_record_hash_drift] candidate_id 来源记录已永久冻结"
                )
            if (by_id["family"], by_id["formula"], by_id["params_json"]) != (
                cand.family, cand.formula, params_json,
            ):
                raise ValueError(
                    "[candidate_identity_drift] candidate_id 配方身份已永久冻结"
                )
            return by_id
        return c.execute(
            """SELECT factor_id,family,formula,params_json,research_epoch,
                      research_record_sha256
                 FROM factors
                WHERE family=? AND formula=? AND params_json=?
                  AND research_record_sha256=?""",
            (cand.family, cand.formula, params_json, research_sha256),
        ).fetchone()

    existing = frozen_identity()
    fid = existing["factor_id"] if existing is not None else cand.factor_id
    now = _now()
    if (existing is not None and existing["research_epoch"] is not None
            and existing["research_epoch"] != config.RESEARCH_EPOCH):
        _reset_for_research_epoch(c, fid, now)
    row = {
        "factor_id": fid,
        "name": cand.name,
        "family": cand.family,
        "formula": cand.formula,
        "description": cand.description,
        "direction": cand.direction,
        "data_requires": json.dumps(list(cand.data_requires), ensure_ascii=False),
        "kind": cand.kind,
        "params_json": params_json,
        "research_record_json": research_json,
        "research_record_sha256": research_sha256,
        "updated_at": now,
    }
    if not existing:
        row["status"] = "candidate"
        row["created_at"] = now
    if existing is not None:
        _upsert(c, row)
        return fid
    keys = list(row)
    try:
        c.execute(
            f"INSERT INTO factors ({', '.join(keys)}) "
            f"VALUES ({', '.join('?' for _ in keys)})",
            [row[key] for key in keys],
        )
    except sqlite3.IntegrityError:
        # 两个进程可能同时通过 SELECT；主键与来源版本唯一键最终仲裁。
        existing = frozen_identity()
        if existing is None:
            raise
        fid = existing["factor_id"]
        if (existing["research_epoch"] is not None
                and existing["research_epoch"] != config.RESEARCH_EPOCH):
            _reset_for_research_epoch(c, fid, now)
        row["factor_id"] = fid
        row.pop("status", None)
        row.pop("created_at", None)
        _upsert(c, row)
    return fid


def register_candidate(c: sqlite3.Connection, cand) -> str:
    """写入普通 runnable 候选；来源 hash 是永久身份。"""
    verify_candidate_source(cand)
    return _register_candidate_verified(c, cand)


def register_blind_candidate(
    c: sqlite3.Connection, cand, authorization_sha256: str,
) -> str:
    """Register one built-locked candidate without weakening the normal gate."""
    verify_blind_candidate_source(cand, authorization_sha256, c)
    return _register_candidate_verified(c, cand)


def candidate_signature(cand) -> tuple[str, str, str, str]:
    """四字段配方签名；保持已冻结 pending batch schema v3 可重放。"""
    params = getattr(cand, "params", {})
    params_json = json.dumps(
        params if isinstance(params, dict) else {},
        ensure_ascii=False, sort_keys=True,
    )
    return (
        str(getattr(cand, "kind", "")),
        str(getattr(cand, "family", "")),
        str(getattr(cand, "formula", "")),
        params_json,
    )


def candidate_trial_signature(cand) -> tuple[str, str, str, str, str]:
    """包含来源冻结 hash 的完整科学试验身份。"""
    return (
        *candidate_signature(cand),
        str(getattr(cand, "research_record_sha256", "") or ""),
    )


def explored_candidate_signatures(c: sqlite3.Connection, epoch: str) -> set[tuple]:
    """返回当前冻结研究 epoch 已实际尝试过的精确候选。"""
    rows = c.execute(
        "SELECT kind,family,formula,params_json,research_record_sha256 "
        "FROM factors WHERE research_epoch=?",
        (epoch,),
    ).fetchall()
    return {
        (str(row["kind"] or ""), str(row["family"] or ""),
         str(row["formula"] or ""), str(row["params_json"] or "{}"),
         str(row["research_record_sha256"] or ""))
        for row in rows
    }


def mark_candidate_explored(c: sqlite3.Connection, fid: str, *, epoch: str,
                            data_key: str) -> None:
    """标记候选已在固定研究设计下计算；成功与失败都不得无限重跑。"""
    c.execute(
        "UPDATE factors SET research_epoch=?,validation_data_key=?,updated_at=? "
        "WHERE factor_id=?",
        (epoch, data_key, _now(), fid),
    )


def _canonical_json(value) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def _json_sha256(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


_BLIND_RUN_HASH = re.compile(r"[0-9a-f]{64}\Z")
_BLIND_RUN_ACTIVE_STATES = frozenset({"reserved", "running"})
_BLIND_RUN_IDENTITY_FIELDS = (
    "authorization_path", "authorization_sha256", "candidate_id",
    "candidate_research_record_sha256", "implementation_identity_sha256",
    "implementation_audit_sha256", "evaluator_fingerprint", "panel_path",
    "panel_key", "panel_sha256", "context_json", "context_sha256",
)


def _blind_run_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"blind run {label} 缺失")
    return value


def _blind_run_hash(value: str, label: str) -> str:
    if not isinstance(value, str) or _BLIND_RUN_HASH.fullmatch(value) is None:
        raise ValueError(f"blind run {label} 必须是小写 SHA-256")
    return value


def _blind_run_context(context: dict) -> tuple[str, str]:
    if not isinstance(context, dict):
        raise ValueError("blind run context 必须是对象")
    try:
        body = _canonical_json(context)
    except (TypeError, ValueError) as exc:
        raise ValueError("blind run context 必须可规范化为 JSON") from exc
    return body, _json_sha256(body)


def _decode_blind_run(row: dict | sqlite3.Row) -> dict:
    out = dict(row)
    body = out.get("context_json")
    if not isinstance(body, str) or _json_sha256(body) != out.get("context_sha256"):
        raise RuntimeError(
            f"blind run context 哈希校验失败: {out.get('candidate_id', '?')}"
        )
    try:
        context = json.loads(body)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"blind run context JSON 损坏: {out.get('candidate_id', '?')}"
        ) from exc
    if not isinstance(context, dict):
        raise RuntimeError(
            f"blind run context 结构损坏: {out.get('candidate_id', '?')}"
        )
    out["context"] = context
    return out


def _blind_run_for_authorization(
    c: sqlite3.Connection, authorization_sha256: str,
) -> dict | None:
    rows = _query_dicts(
        c,
        "SELECT * FROM candidate_blind_runs WHERE authorization_sha256=?",
        (authorization_sha256,),
    )
    return _decode_blind_run(rows[0]) if rows else None


def blind_run_for_candidate(
    c: sqlite3.Connection, candidate_id: str,
) -> dict | None:
    """Return the candidate's permanent blind-run claim, if any."""
    candidate_id = _blind_run_text(candidate_id, "candidate_id")
    rows = _query_dicts(
        c, "SELECT * FROM candidate_blind_runs WHERE candidate_id=?",
        (candidate_id,),
    )
    return _decode_blind_run(rows[0]) if rows else None


def _begin_blind_run_write(c: sqlite3.Connection) -> None:
    if c.in_transaction:
        raise RuntimeError("blind run 写入要求独立事务")
    c.execute("BEGIN IMMEDIATE")


def _consume_active_blind_run(
    c: sqlite3.Connection, row: dict, reason: str,
) -> dict:
    if row["state"] not in _BLIND_RUN_ACTIVE_STATES:
        raise RuntimeError("blind run 已终态，不能保守消费")
    updated = c.execute(
        "UPDATE candidate_blind_runs "
        "SET state='consumed_unknown',completed_at=?,terminal_reason=? "
        "WHERE authorization_sha256=? AND state IN ('reserved','running')",
        (_now(), reason, row["authorization_sha256"]),
    )
    if updated.rowcount != 1:
        raise RuntimeError("blind run 保守消费未写入")
    consumed = _blind_run_for_authorization(c, row["authorization_sha256"])
    if consumed is None:
        raise RuntimeError("blind run 保守消费后记录丢失")
    return consumed


def reserve_blind_run(
    c: sqlite3.Connection, *, candidate_id: str, authorization_path: str,
    authorization_sha256: str, candidate_research_record_sha256: str,
    implementation_identity_sha256: str,
    implementation_audit_sha256: str, evaluator_fingerprint: str,
    panel_path: str, panel_key: str, panel_sha256: str,
    context: dict,
) -> dict:
    """Permanently claim one candidate before any blind Panel I/O."""
    context_json, context_sha256 = _blind_run_context(context)
    identity = {
        "candidate_id": _blind_run_text(candidate_id, "candidate_id"),
        "authorization_path": _blind_run_text(
            authorization_path, "authorization_path",
        ),
        "authorization_sha256": _blind_run_hash(
            authorization_sha256, "authorization_sha256",
        ),
        "candidate_research_record_sha256": _blind_run_hash(
            candidate_research_record_sha256,
            "candidate_research_record_sha256",
        ),
        "implementation_identity_sha256": _blind_run_hash(
            implementation_identity_sha256, "implementation_identity_sha256",
        ),
        "implementation_audit_sha256": _blind_run_hash(
            implementation_audit_sha256, "implementation_audit_sha256",
        ),
        "evaluator_fingerprint": _blind_run_hash(
            evaluator_fingerprint, "evaluator_fingerprint",
        ),
        "panel_path": _blind_run_text(panel_path, "panel_path"),
        "panel_key": _blind_run_text(panel_key, "panel_key"),
        "panel_sha256": _blind_run_hash(panel_sha256, "panel_sha256"),
        "context_json": context_json,
        "context_sha256": context_sha256,
    }
    _begin_blind_run_write(c)
    try:
        existing = _blind_run_for_authorization(
            c, identity["authorization_sha256"],
        )
        if existing is not None:
            if any(existing[field] != identity[field]
                   for field in _BLIND_RUN_IDENTITY_FIELDS):
                if existing["state"] in _BLIND_RUN_ACTIVE_STATES:
                    result = _consume_active_blind_run(
                        c, existing,
                        "同一 blind authorization 的冻结身份或上下文漂移",
                    )
                    c.commit()
                    return result
                raise ValueError("blind run 终态授权身份不一致")
            c.commit()
            return existing

        claimed = blind_run_for_candidate(c, identity["candidate_id"])
        if claimed is not None:
            raise ValueError(
                f"candidate 已永久绑定另一 blind authorization: "
                f"{identity['candidate_id']}"
            )
        reserved_at = _now()
        c.execute(
            """INSERT INTO candidate_blind_runs
                 (authorization_sha256,authorization_path,candidate_id,
                  candidate_research_record_sha256,
                  implementation_identity_sha256,
                  implementation_audit_sha256,evaluator_fingerprint,
                  panel_path,panel_key,panel_sha256,context_json,context_sha256,
                  state,reserved_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                identity["authorization_sha256"],
                identity["authorization_path"], identity["candidate_id"],
                identity["candidate_research_record_sha256"],
                identity["implementation_identity_sha256"],
                identity["implementation_audit_sha256"],
                identity["evaluator_fingerprint"], identity["panel_path"],
                identity["panel_key"], identity["panel_sha256"],
                identity["context_json"], identity["context_sha256"],
                "reserved", reserved_at,
            ),
        )
        result = _blind_run_for_authorization(
            c, identity["authorization_sha256"],
        )
        if result is None:
            raise RuntimeError("blind run 预约后记录丢失")
        c.commit()
        return result
    except Exception:
        if c.in_transaction:
            c.rollback()
        raise


def mark_blind_run_started(
    c: sqlite3.Connection, authorization_sha256: str, *, context: dict,
) -> dict:
    """Move an exact reservation to running; exact retries are idempotent."""
    authorization_sha256 = _blind_run_hash(
        authorization_sha256, "authorization_sha256",
    )
    context_json, context_sha256 = _blind_run_context(context)
    _begin_blind_run_write(c)
    try:
        row = _blind_run_for_authorization(c, authorization_sha256)
        if row is None:
            raise ValueError("blind run authorization 未预约")
        if (row["context_json"] != context_json
                or row["context_sha256"] != context_sha256):
            if row["state"] in _BLIND_RUN_ACTIVE_STATES:
                result = _consume_active_blind_run(
                    c, row, "blind run 启动或恢复上下文漂移",
                )
                c.commit()
                return result
            raise ValueError("blind run 终态上下文不一致")
        if row["state"] != "reserved":
            c.commit()
            return row
        c.execute(
            "UPDATE candidate_blind_runs SET state='running',started_at=? "
            "WHERE authorization_sha256=? AND state='reserved'",
            (_now(), authorization_sha256),
        )
        result = _blind_run_for_authorization(c, authorization_sha256)
        if result is None or result["state"] != "running":
            raise RuntimeError("blind run 未能进入 running")
        c.commit()
        return result
    except Exception:
        if c.in_transaction:
            c.rollback()
        raise


def complete_blind_run(
    c: sqlite3.Connection, authorization_sha256: str, *, context: dict,
    evidence_path: str, evidence_sha256: str,
) -> dict:
    """Freeze one completed result; a different replay can never replace it."""
    authorization_sha256 = _blind_run_hash(
        authorization_sha256, "authorization_sha256",
    )
    context_json, context_sha256 = _blind_run_context(context)
    evidence_path = _blind_run_text(evidence_path, "evidence_path")
    evidence_sha256 = _blind_run_hash(evidence_sha256, "evidence_sha256")
    _begin_blind_run_write(c)
    try:
        row = _blind_run_for_authorization(c, authorization_sha256)
        if row is None:
            raise ValueError("blind run authorization 未预约")
        if (row["context_json"] != context_json
                or row["context_sha256"] != context_sha256):
            if row["state"] in _BLIND_RUN_ACTIVE_STATES:
                result = _consume_active_blind_run(
                    c, row, "blind run 完成时上下文漂移",
                )
                c.commit()
                return result
            raise ValueError("blind run 终态上下文不一致")
        if row["state"] == "complete":
            if (row["result_evidence_path"] != evidence_path
                    or row["result_evidence_sha256"] != evidence_sha256):
                raise ValueError("blind run 完成证据已冻结")
            c.commit()
            return row
        if row["state"] == "consumed_unknown":
            raise ValueError("blind run 已保守消费，不能补写完成结果")
        if row["state"] != "running":
            raise RuntimeError("blind run 必须先进入 running")
        c.execute(
            "UPDATE candidate_blind_runs "
            "SET state='complete',result_evidence_path=?,"
            "result_evidence_sha256=?,completed_at=? "
            "WHERE authorization_sha256=? AND state='running'",
            (evidence_path, evidence_sha256, _now(), authorization_sha256),
        )
        result = _blind_run_for_authorization(c, authorization_sha256)
        if result is None or result["state"] != "complete":
            raise RuntimeError("blind run 未能进入 complete")
        c.commit()
        return result
    except Exception:
        if c.in_transaction:
            c.rollback()
        raise


def consume_blind_run_unknown(
    c: sqlite3.Connection, authorization_sha256: str, *, context: dict,
    reason: str,
) -> dict:
    """Terminally consume an unreplayable reservation without inventing a result."""
    authorization_sha256 = _blind_run_hash(
        authorization_sha256, "authorization_sha256",
    )
    context_json, context_sha256 = _blind_run_context(context)
    reason = _blind_run_text(reason, "terminal_reason")
    _begin_blind_run_write(c)
    try:
        row = _blind_run_for_authorization(c, authorization_sha256)
        if row is None:
            raise ValueError("blind run authorization 未预约")
        context_matches = (
            row["context_json"] == context_json
            and row["context_sha256"] == context_sha256
        )
        if row["state"] == "consumed_unknown":
            if not context_matches or row["terminal_reason"] != reason:
                raise ValueError("blind run consumed_unknown 终态已冻结")
            c.commit()
            return row
        if row["state"] == "complete":
            raise ValueError("blind run 已完成，不能改为 consumed_unknown")
        result = _consume_active_blind_run(c, row, reason)
        c.commit()
        return result
    except Exception:
        if c.in_transaction:
            c.rollback()
        raise


def _finite(value):
    """Return a JSON-safe finite scalar, preserving non-numeric values."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value if math.isfinite(float(value)) else None
    return value


def _failure_diagnostic(*, reason: str, record: dict | None = None) -> dict:
    """Normalize a terminal rejection into one compact, actionable record."""
    record = record if isinstance(record, dict) else {}
    result = record.get("result") if isinstance(record.get("result"), dict) else {}
    text = str(reason or record.get("reason") or record.get("gate_reason") or "")
    declared_stage = str(result.get("stage") or "")
    if text.startswith("compute:"):
        stage = "compute"
    elif text.startswith("新颖性归档:") or text.startswith("新颖性归档："):
        stage = "archive"
    elif text.startswith("L0:") or declared_stage == "L0":
        stage = "L0"
    elif text.startswith("L1:") or declared_stage == "L1":
        stage = "L1"
    elif text.startswith("L2:") or declared_stage == "L2":
        stage = "L2"
    elif text.startswith("L3"):
        stage = "L3"
    elif text.startswith("M1.4"):
        stage = "prescreen"
    else:
        stage = declared_stage or "terminal"

    detail = result.get(stage) if isinstance(result.get(stage), dict) else {}
    missing_fields = []
    for source in (record, result, detail):
        raw = source.get("missing_fields") if isinstance(source, dict) else None
        if isinstance(raw, (list, tuple)):
            missing_fields.extend(str(value) for value in raw if value)

    metric = "gate_passed"
    observed = False
    threshold = True
    comparator = "=="
    attribution = "terminal_gate_rejection"
    valid_count = eligible_count = None
    next_action = "保留本次失败证据；只提交预注册的新经济假设，不放松当前门槛。"

    if stage == "compute":
        metric, attribution = "factor_non_empty", "compute_empty_or_failed"
        next_action = "修复该配方的数据字段或计算实现，确认能产生有限值后再以新输入身份运行。"
    elif stage == "archive":
        novelty = record.get("novelty") if isinstance(record.get("novelty"), dict) else {}
        corr = _finite(novelty.get("ic_corr_nearest"))
        if corr is not None:
            metric, observed = "abs_ic_corr_nearest", corr
            threshold, comparator = float(config.DAEMON_NOVELTY_CORR), "<"
            attribution = "signal_family_duplicate"
        else:
            metric, attribution = "formula_unseen", "formula_already_explored"
        next_action = "从公开来源预注册不同经济机制的配方；不要改名或微调旧公式绕过去重。"
    elif stage == "L0":
        valid = detail.get("valid_observations")
        eligible = detail.get("eligible_observations")
        if "覆盖率" in text:
            metric, observed, threshold = "coverage", _finite(detail.get("coverage")), 0.60
            comparator, attribution = ">=", "eligible_signal_coverage"
        elif "恒定" in text:
            metric = "variable_days"
            observed = _finite(detail.get("variable_days"))
            required = detail.get("required_variable_days")
            if required is None:
                total_days = int(detail.get("total_days") or 0)
                required = max(1, int(0.5 * total_days))
            threshold = _finite(required)
            comparator, attribution = ">=", "cross_section_nearly_constant"
        else:
            effective_dates = detail.get("candidate_cross_section_days")
            if effective_dates is None:
                effective_dates = detail.get("n_dates")
            metric, observed = "effective_ic_dates", _finite(effective_dates)
            threshold, comparator = int(config.MIN_IC_DATES), ">="
            attribution = "insufficient_effective_dates"
        valid_count, eligible_count = _finite(valid), _finite(eligible)
        next_action = "先修复点时字段覆盖或横截面变化来源，再重建候选；覆盖率和样本门槛保持不变。"
    elif stage == "L1":
        if "|IC_IR|" in text:
            metric, observed = "abs_ic_ir", _finite(abs(detail.get("ic_ir"))) if detail.get("ic_ir") is not None else None
            threshold, attribution = float(config.IC_IR_THRESHOLD), "weak_ic_stability"
        elif "分层利差" in text:
            value = detail.get("long_short_annualized")
            metric, observed = "abs_long_short_annualized", _finite(abs(value)) if value is not None else None
            threshold, attribution = float(config.MIN_QUANTILE_SPREAD), "insufficient_quantile_spread"
        else:
            value = detail.get("ic_mean")
            metric, observed = "abs_ic_mean", _finite(abs(value)) if value is not None else None
            threshold, attribution = float(config.IC_THRESHOLD), "weak_discovery_ic"
        comparator = ">="
        valid_count = _finite(detail.get("n_dates"))
        l0_detail = result.get("L0") or {}
        eligible_days = l0_detail.get("eligible_cross_section_days")
        if eligible_days is None:
            eligible_days = l0_detail.get("total_days")
        eligible_count = _finite(eligible_days)
        next_action = "终止当前配方规格并预注册不同经济机制；不得围绕 discovery 结果下调 IC 门槛。"
    elif stage == "L2":
        metric = "validation_gate_passed"
        observed, threshold, comparator = False, True, "=="
        attribution = "independent_validation_failed"
        valid_count = _finite(detail.get("oos_n_dates"))
        eligible_count = None
        next_action = "封存当前假设；仅用未触碰的新机制建立下一实验，不复用 validation 调参。"
    elif stage == "L3":
        match = re.search(
            r"validation p=([0-9.eE+-]+)\s*>\s*([0-9.eE+-]+|nan)", text,
        )
        metric, attribution = "validation_p_value", "fdr_not_rejected"
        observed = _finite(float(match.group(1))) if match else None
        threshold = _finite(float(match.group(2))) if match and match.group(2) != "nan" else None
        comparator = "<="
        next_action = "保留完整 FDR 失败族；下一轮只加入预注册的新假设，不删除失败项或放宽 q。"
    return {
        "stage": stage,
        "metric": metric,
        "observed": _finite(observed),
        "threshold": _finite(threshold),
        "comparator": comparator,
        "valid_count": _finite(valid_count),
        "eligible_count": _finite(eligible_count),
        "missing_fields": sorted(set(missing_fields)),
        "attribution": attribution,
        "reason": text,
        "next_action": next_action,
    }


def _decode_mining_batch(row: sqlite3.Row) -> dict:
    family_body = row["family_json"]
    if _json_sha256(family_body) != row["family_sha256"]:
        raise RuntimeError(f"mining batch family 哈希校验失败: {row['batch_id']}")
    try:
        family = json.loads(family_body)
        context = json.loads(row["context_json"])
        progress = json.loads(row["progress_json"])
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"mining batch ledger JSON 损坏: {row['batch_id']}") from exc
    if (not isinstance(family, list) or not isinstance(context, dict)
            or not isinstance(progress, dict)):
        raise RuntimeError(f"mining batch ledger 结构损坏: {row['batch_id']}")
    fdr_input = None
    if row["fdr_input_json"] is not None:
        if (_json_sha256(row["fdr_input_json"])
                != row["fdr_input_sha256"]):
            raise RuntimeError(f"mining batch FDR family 哈希校验失败: {row['batch_id']}")
        try:
            fdr_input = json.loads(row["fdr_input_json"])
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"mining batch FDR JSON 损坏: {row['batch_id']}") from exc
        if not isinstance(fdr_input, list):
            raise RuntimeError(f"mining batch FDR family 结构损坏: {row['batch_id']}")
    out = dict(row)
    out.update({"family": family, "context": context,
                "progress": progress, "fdr_input": fdr_input})
    return out


def pending_mining_batch(c: sqlite3.Connection, *, epoch: str) -> dict | None:
    """取唯一待恢复批次；不同 epoch 的遗留批次必须先由迁移显式处置。"""
    rows = c.execute(
        "SELECT * FROM mining_batches WHERE status='pending' ORDER BY created_at,batch_id"
    ).fetchall()
    stale = [row["batch_id"] for row in rows if row["research_epoch"] != epoch]
    if stale:
        raise RuntimeError(
            "存在其他 research_epoch 的 pending mining batch，已 fail-closed: "
            + ",".join(stale)
        )
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError(f"同 epoch 存在多个 pending mining batch: {epoch}")
    return _decode_mining_batch(rows[0])


def create_mining_batch(c: sqlite3.Connection, *, batch_id: str, epoch: str,
                        data_key: str, family: list[dict],
                        context: dict | None = None) -> dict:
    """在验证前冻结完整候选族；family 此后只能读，不能缩小或替换。"""
    pending = pending_mining_batch(c, epoch=epoch)
    body = _canonical_json(family)
    digest = _json_sha256(body)
    context_body = _canonical_json(context or {})
    if pending is not None:
        if (pending["batch_id"] != batch_id or pending["data_key"] != data_key
                or pending["family_sha256"] != digest
                or pending["context_json"] != context_body):
            raise RuntimeError(f"已有不同的 pending mining batch: {pending['batch_id']}")
        return pending
    c.execute(
        """INSERT INTO mining_batches
             (batch_id,research_epoch,data_key,family_json,family_sha256,
              context_json,progress_json,status,created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (batch_id, epoch, data_key, body, digest, context_body,
         "{}", "pending", _now()),
    )
    return _decode_mining_batch(c.execute(
        "SELECT * FROM mining_batches WHERE batch_id=?", (batch_id,),
    ).fetchone())


def set_mining_candidate_progress(c: sqlite3.Connection, batch_id: str,
                                  position: int, record: dict) -> dict:
    """幂等推进单个候选；terminal 结果不可再改。"""
    row = c.execute(
        "SELECT * FROM mining_batches WHERE batch_id=?", (batch_id,),
    ).fetchone()
    if row is None or row["status"] != "pending":
        raise RuntimeError(f"mining batch 不可写: {batch_id}")
    batch = _decode_mining_batch(row)
    if position < 0 or position >= len(batch["family"]):
        raise IndexError(f"mining candidate position 越界: {position}")
    key = str(position)
    old = batch["progress"].get(key)
    if old is not None:
        if old == record:
            return batch
        if old.get("state") == "terminal":
            raise ValueError(f"mining candidate terminal 结果已冻结: {batch_id}/{position}")
        if old.get("state") != "novelty_decided" or record.get("state") != "terminal":
            raise ValueError(f"mining candidate 非法状态迁移: {batch_id}/{position}")
    batch["progress"][key] = record
    c.execute(
        "UPDATE mining_batches SET progress_json=? WHERE batch_id=? AND status='pending'",
        (_canonical_json(batch["progress"]), batch_id),
    )
    if record.get("state") == "terminal" and record.get("factor_id"):
        factor = c.execute(
            "SELECT status,status_reason FROM factors WHERE factor_id=?",
            (record["factor_id"],),
        ).fetchone()
        if factor is not None and factor["status"] == "rejected":
            failure = _failure_diagnostic(
                reason=factor["status_reason"] or "", record=record,
            )
            c.execute(
                "UPDATE factors SET failure_json=? WHERE factor_id=?",
                (_canonical_json(failure), record["factor_id"]),
            )
            _append_factor_trial_snapshot(
                c, record["factor_id"], event="terminal_mining_rejection",
                details={
                    "batch_id": batch_id, "position": position,
                    "record": record,
                },
            )
    return batch


def freeze_mining_fdr_family(c: sqlite3.Connection, batch_id: str,
                             fdr_input: list[dict]) -> dict:
    """全部候选 terminal 后一次冻结 BH 输入；重启只允许完全相同的重放。"""
    row = c.execute(
        "SELECT * FROM mining_batches WHERE batch_id=?", (batch_id,),
    ).fetchone()
    if row is None or row["status"] != "pending":
        raise RuntimeError(f"mining batch 不可冻结 FDR: {batch_id}")
    batch = _decode_mining_batch(row)
    if (len(batch["progress"]) != len(batch["family"])
            or any(item.get("state") != "terminal"
                   for item in batch["progress"].values())):
        raise RuntimeError(f"mining batch 尚有未完成候选: {batch_id}")
    expected = []
    for position in range(len(batch["family"])):
        record = batch["progress"][str(position)]
        if record.get("outcome") == "validation_tested":
            expected.append({
                "factor_id": record["factor_id"],
                "p_value": record["validation_p_value"],
                "p_value_method": record.get("validation_p_value_method"),
                "p_value_n_effective": record.get(
                    "validation_p_value_n_effective"
                ),
                "gate_passed": bool(record.get("gate_passed")),
            })
    if fdr_input != expected:
        # 数据/参考快照在崩溃恢复期间已变化时，允许唯一一种保守变化：
        # 身份与已观察 p 完全不变，gate 只能从 True 降为 False，并冻结
        # 非空 recovery_error。绝不允许改 p、缩族、换序或把 False 升 True。
        conservative_ok = len(fdr_input) == len(expected)
        immutable_keys = (
            "factor_id", "p_value", "p_value_method", "p_value_n_effective",
        )
        for supplied, original in zip(fdr_input, expected):
            if not conservative_ok:
                break
            reason = supplied.get("recovery_error")
            if (not isinstance(reason, str) or not reason.strip()
                    or any(supplied.get(key) != original.get(key)
                           for key in immutable_keys)
                    or (bool(supplied.get("gate_passed"))
                        and not bool(original.get("gate_passed")))
                    or set(supplied) - set(original) != {"recovery_error"}):
                conservative_ok = False
        if not conservative_ok:
            raise ValueError(f"mining batch FDR family 不完整或顺序变化: {batch_id}")
    factor_ids = [str(item["factor_id"]) for item in fdr_input]
    if len(factor_ids) != len(set(factor_ids)):
        raise ValueError(f"mining batch FDR family factor_id 重复: {batch_id}")
    body = _canonical_json(fdr_input)
    digest = _json_sha256(body)
    if batch["fdr_input"] is not None:
        if (row["fdr_input_json"] != body
                or row["fdr_input_sha256"] != digest):
            raise ValueError(f"mining batch FDR family 已冻结但输入变化: {batch_id}")
        return batch
    c.execute(
        """UPDATE mining_batches
              SET fdr_input_json=?,fdr_input_sha256=?
            WHERE batch_id=? AND status='pending' AND fdr_input_json IS NULL""",
        (body, digest, batch_id),
    )
    return _decode_mining_batch(c.execute(
        "SELECT * FROM mining_batches WHERE batch_id=?", (batch_id,),
    ).fetchone())


def complete_mining_batch(c: sqlite3.Connection, batch_id: str) -> None:
    """仅在 FDR 全族落库/配方 seal 完成后终结 ledger。"""
    row = c.execute(
        "SELECT status,fdr_input_json FROM mining_batches WHERE batch_id=?",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise RuntimeError(f"mining batch 不存在: {batch_id}")
    if row["status"] == "complete":
        return
    if row["status"] != "pending" or row["fdr_input_json"] is None:
        raise RuntimeError(f"mining batch 尚不可完成: {batch_id}")
    c.execute(
        "UPDATE mining_batches SET status='complete',completed_at=? WHERE batch_id=?",
        (_now(), batch_id),
    )


def recover_interrupted_backtests(c: sqlite3.Connection | None = None) -> dict:
    """把进程中断遗留的 backtesting 任务重新排入 registered 队列。

    回测会在耗时计算前提交 ``backtesting`` 状态；进程若在计算期间退出，该状态
    不会自行消失。daemon 单例锁取得后调用本函数，确保只有新任 owner 做恢复。
    原状态原因和时间写入新原因，保留可追溯性。
    """
    own = c is None
    conn = c if c is not None else _conn()
    recovered_at = _now()
    try:
        rows = conn.execute(
            "SELECT factor_id, status_reason, updated_at FROM factors WHERE status='backtesting'"
        ).fetchall()
        factor_ids = []
        for row in rows:
            fid = row["factor_id"]
            old_reason = row["status_reason"] or "未记录"
            old_updated_at = row["updated_at"] or "未记录"
            reason = (
                f"daemon 启动恢复于 {recovered_at}：检测到上次进程中断，"
                f"遗留 backtesting 任务已重新排队；原状态时间={old_updated_at}；"
                f"原原因={old_reason}"
            )
            conn.execute(
                "UPDATE factors SET status='registered', status_reason=?, updated_at=? "
                "WHERE factor_id=? AND status='backtesting'",
                (reason, recovered_at, fid),
            )
            factor_ids.append(fid)
        if own:
            conn.commit()
        return {
            "count": len(factor_ids),
            "factor_ids": factor_ids,
            "recovered_at": recovered_at,
        }
    finally:
        if own:
            conn.close()


_PRODUCTION_ARTIFACT_KEYS = (
    "path", "sha256", "data_key", "as_of", "evaluator_fingerprint",
)


def _production_manifest(c: sqlite3.Connection, epoch: str) -> list[dict]:
    """返回当前可消费 production 集合；缺任一制品字段即 fail-closed。"""
    rows = c.execute(
        """SELECT factor_id,sealed_recipe_sha256,research_record_sha256,
                  production_admission_path,production_admission_sha256,
                  production_score_path,
                  production_score_sha256,production_data_key,production_as_of,
                  production_evaluator_sha256
             FROM factors
            WHERE status='production' AND research_epoch=?
            ORDER BY factor_id""",
        (epoch,),
    ).fetchall()
    manifest = []
    for row in rows:
        item = dict(row)
        required = (
            "factor_id", "sealed_recipe_sha256", "research_record_sha256",
            "production_admission_path", "production_admission_sha256",
            "production_score_path",
            "production_score_sha256", "production_data_key", "production_as_of",
            "production_evaluator_sha256",
        )
        if any(not item.get(key) for key in required):
            raise RuntimeError(
                f"production 因子缺少可消费分数制品: {item.get('factor_id')}"
            )
        manifest.append(item)
    return manifest


def _refresh_production_catalog(c: sqlite3.Connection, epoch: str) -> dict:
    """在调用方事务内刷新 production manifest 与内容版本。"""
    manifest = _production_manifest(c, epoch)
    body = _canonical_json(manifest)
    version = _json_sha256(body)
    c.execute(
        """INSERT INTO production_catalog
             (catalog_id,research_epoch,catalog_version,manifest_json,updated_at)
           VALUES (1,?,?,?,?)
           ON CONFLICT(catalog_id) DO UPDATE SET
             research_epoch=excluded.research_epoch,
             catalog_version=excluded.catalog_version,
             manifest_json=excluded.manifest_json,
             updated_at=excluded.updated_at""",
        (epoch, version, body, _now()),
    )
    return {
        "research_epoch": epoch,
        "catalog_version": version,
        "manifest": manifest,
    }


def production_catalog(c: sqlite3.Connection, epoch: str) -> dict | None:
    """读取并复核当前 catalog；因子状态漂移时不返回陈旧目录。"""
    row = c.execute(
        "SELECT * FROM production_catalog WHERE catalog_id=1 AND research_epoch=?",
        (epoch,),
    ).fetchone()
    if row is None:
        return None
    manifest = _production_manifest(c, epoch)
    body = _canonical_json(manifest)
    if body != row["manifest_json"] or _json_sha256(body) != row["catalog_version"]:
        raise RuntimeError("production catalog 与当前注册表状态不一致")
    return {
        "research_epoch": epoch,
        "catalog_version": row["catalog_version"],
        "manifest": manifest,
        "updated_at": row["updated_at"],
    }


def set_status(c: sqlite3.Connection, fid: str, status: str, reason: str = ""):
    old = c.execute(
        "SELECT status FROM factors WHERE factor_id=?", (fid,),
    ).fetchone()
    if old is None:
        raise ValueError(f"factor 不存在: {fid}")
    old_status = old["status"] if isinstance(old, sqlite3.Row) else old[0]
    if status == "production" and old_status != "production":
        raise ValueError("禁止绕过生产制品门禁写入 production")
    now = _now()
    if status == "rejected" and reason.startswith(
        ("compute:", "L0:", "L1:", "新颖性归档:")
    ):
        try:
            _clear_result_evidence(c, fid)
        except sqlite3.OperationalError as exc:
            if "no such column" not in str(exc):
                raise
    failure_json = (
        _canonical_json(_failure_diagnostic(reason=reason))
        if status == "rejected" else None
    )
    try:
        c.execute(
            "UPDATE factors SET status=?, status_reason=?, failure_json=?, updated_at=? "
            "WHERE factor_id=?",
            (status, reason, failure_json, now, fid),
        )
    except sqlite3.OperationalError as exc:
        # A few narrow recovery tools/tests intentionally open a pre-migration
        # compatibility schema. The real registry always runs migrate_schema.
        if "no such column: failure_json" not in str(exc):
            raise
        c.execute(
            "UPDATE factors SET status=?, status_reason=?, updated_at=? WHERE factor_id=?",
            (status, reason, now, fid),
        )
    if status in _TERMINAL_FACTOR_STATUSES:
        _append_factor_trial_snapshot(
            c, fid, event=f"terminal_status:{status}",
        )
    if old_status == "production" and status != "production":
        epoch_row = c.execute(
            "SELECT research_epoch FROM factors WHERE factor_id=?", (fid,),
        ).fetchone()
        epoch = (epoch_row["research_epoch"] if isinstance(epoch_row, sqlite3.Row)
                 else epoch_row[0])
        # catalog_id=1 只代表当前研究 epoch。清理历史 epoch 状态绝不能
        # 把当前目录覆盖成旧 epoch manifest。
        if epoch == config.RESEARCH_EPOCH:
            _refresh_production_catalog(c, config.RESEARCH_EPOCH)


def set_validation(c: sqlite3.Connection, fid: str, res: dict):
    now = _now()
    # M1.5 证据列（soft/报告层，不参与状态机判定）
    diag = res.get("diag") or {}
    mono = diag.get("monotonic") or {}
    decay = diag.get("ic_decay") or {}
    turn = diag.get("turnover") or {}
    cap = diag.get("capacity") or {}
    diag_json = None
    try:
        diag_json = json.dumps(diag, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        diag_json = None
    c.execute(
        """UPDATE factors SET
             status=?, status_reason=?, is_ic_mean=?, is_ic_ir=?, oos_ic_mean=?,
             oos_ic_ir=?, ic_retention=?, long_short_annualized=?, p_value=?,
             raw_ic_mean=?, raw_ic_ir=?, neutralize_mode=?,
             monotonic_aligned=?, ic_half_life=?, turnover_ratio=?,
             capacity_estimate=?, incremental_ic=?, diag_json=?, failure_json=NULL,
             research_epoch=?,
             validation_data_key=?,
             updated_at=?
           WHERE factor_id=?""",
        (
            res.get("status", "validated"), res.get("status_reason", ""),
            res.get("is_ic_mean"), res.get("is_ic_ir"),
            res.get("oos_ic_mean"), res.get("oos_ic_ir"),
            res.get("ic_retention"), res.get("long_short_annualized"),
            res.get("p_value"),
            res.get("raw_ic_mean"), res.get("raw_ic_ir"),
            res.get("neutralize_mode"),
            mono.get("monotonic_aligned"), decay.get("half_life"),
            turn.get("turnover_ratio"), cap.get("capacity_estimate_yi"),
            res.get("incremental_ic"), diag_json, res.get("research_epoch"),
            res.get("validation_data_key"),
            now, fid,
        ),
    )


def seal_factor(c: sqlite3.Connection, fid: str, *, sealed_at: str,
                sealed_data_end: str, recipe_sha256: str,
                research_epoch: str) -> dict:
    """不可变地冻结一个研究假设及其当时可见数据截止日。

    重启可以用完全相同的输入幂等恢复；任何字段变化都说明有人试图在同一
    factor_id 下改配方或移动前向起点，必须 fail-closed。
    """
    row = c.execute(
        "SELECT research_epoch,sealed_at,sealed_data_end,sealed_recipe_sha256 "
        "FROM factors WHERE factor_id=?", (fid,),
    ).fetchone()
    if row is None:
        raise ValueError(f"factor 不存在，无法冻结: {fid}")
    existing = tuple(row[k] for k in (
        "sealed_at", "sealed_data_end", "sealed_recipe_sha256",
    ))
    requested = (sealed_at, sealed_data_end, recipe_sha256)
    if any(value is not None for value in existing):
        if existing != requested or row["research_epoch"] != research_epoch:
            raise ValueError(f"factor seal 已冻结但输入变化: {fid}")
        return dict(row)
    if row["research_epoch"] != research_epoch:
        raise ValueError(f"factor epoch 不匹配，无法冻结: {fid}")
    cur = c.execute(
        "UPDATE factors SET sealed_at=?,sealed_data_end=?,sealed_recipe_sha256=?,"
        "updated_at=? WHERE factor_id=? AND sealed_at IS NULL "
        "AND sealed_data_end IS NULL AND sealed_recipe_sha256 IS NULL",
        (sealed_at, sealed_data_end, recipe_sha256, _now(), fid),
    )
    if cur.rowcount != 1:
        raise RuntimeError(f"factor seal 并发冻结失败: {fid}")
    return dict(c.execute(
        "SELECT research_epoch,sealed_at,sealed_data_end,sealed_recipe_sha256 "
        "FROM factors WHERE factor_id=?", (fid,),
    ).fetchone())


def _verified_project_file(relative_path: str, expected_sha256: str,
                           *, label: str) -> tuple[Path, dict]:
    root = config.PROJECT_ROOT.resolve()
    path = (root / str(relative_path)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label}路径越出项目目录") from exc
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label}缺失或损坏") from exc
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError(f"{label} hash 不匹配")
    if not isinstance(payload, dict):
        raise ValueError(f"{label}格式无效")
    return path, payload


def recipe_execution_hold_days(recipe: dict) -> int:
    """Return the preregistered holding period; old recipes use the legacy default."""
    execution_spec = recipe.get("execution_spec")
    if execution_spec is None:
        return int(config.HOLD_DAYS)
    if not isinstance(execution_spec, dict):
        raise ValueError("production recipe execution_spec 格式无效")
    hold = execution_spec.get("hold_days")
    if isinstance(hold, bool) or not isinstance(hold, int) or hold < 1:
        raise ValueError("production recipe execution_spec.hold_days 无效")
    return hold


def _load_frozen_recipe(row: dict) -> dict:
    fid = str(row["factor_id"])
    path = config.REGISTRY_DIR / "recipes" / f"{fid}.json"
    try:
        raw = path.read_bytes()
        recipe = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"production recipe 缺失或损坏: {fid}") from exc
    if (not isinstance(recipe, dict)
            or hashlib.sha256(raw).hexdigest() != row.get("sealed_recipe_sha256")
            or recipe.get("research_epoch") != config.RESEARCH_EPOCH
            or recipe.get("sealed_at") != row.get("sealed_at")
            or recipe.get("sealed_data_end") != row.get("sealed_data_end")
            or recipe.get("research_record_sha256")
            != row.get("research_record_sha256")):
        raise ValueError(f"production recipe seal/hash 复核失败: {fid}")
    return recipe


def verify_forward_production_semantics(
    c: sqlite3.Connection,
    row: dict,
    *,
    test_epoch: str,
    evidence_path: str,
    evidence_sha256: str,
    evaluator_fingerprint: str | None = None,
    recipe: dict | None = None,
) -> dict:
    """严格复核可投产前向证据；不把 DB 布尔位当作充分证明。"""
    fid = str(row["factor_id"])
    from .production import forward_test_epoch, process_evaluator_fingerprint
    expected_evaluator = evaluator_fingerprint or process_evaluator_fingerprint()
    expected_epoch = forward_test_epoch(expected_evaluator)
    if test_epoch != expected_epoch:
        raise ValueError(f"production 前向 test epoch 不是当前冻结口径: {fid}")
    if row.get("research_epoch") != config.RESEARCH_EPOCH:
        raise ValueError(f"production 因子不属于当前 research epoch: {fid}")

    frozen = c.execute(
        "SELECT * FROM test_epochs WHERE epoch=?", (test_epoch,),
    ).fetchone()
    if frozen is None or (
        float(frozen["q"]) != float(config.FDR_Q)
        or int(frozen["dependent"]) != 1
        or frozen["method"] != "LOND"
        or frozen["method_version"] != "lond-dependent-v1"
        or frozen["p_value_method"] != "non_overlapping_phase_max_hac"
        or not frozen["initial_data_key"]
        or not frozen["created_at"]
    ):
        raise ValueError(f"production 前向 test epoch 冻结元数据无效: {fid}")

    test = c.execute(
        "SELECT * FROM factor_tests WHERE epoch=? AND factor_id=?",
        (test_epoch, fid),
    ).fetchone()
    if (test is None or not bool(test["gate_passed"])
            or not bool(test["online_reject"])
            or test["evidence_path"] != evidence_path
            or test["evidence_sha256"] != evidence_sha256
            or test["p_value_method"] != frozen["p_value_method"]
            or test["evaluator_fingerprint"] != expected_evaluator
            or not test["data_key"]):
        raise ValueError(f"production 前向 gate/LOND/证据账本未通过: {fid}")
    test = dict(test)
    try:
        test_p = float(test["p_value"])
        alpha_i = float(test["alpha_i"])
        hypothesis_order = int(test["hypothesis_order"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"production 前向 LOND 数值无效: {fid}") from exc
    prior = c.execute(
        """SELECT COUNT(*) AS n,COALESCE(SUM(online_reject),0) AS rejected,
                  MIN(hypothesis_order) AS first_order,
                  MAX(hypothesis_order) AS last_order
             FROM factor_tests
            WHERE epoch=? AND hypothesis_order<?""",
        (test_epoch, hypothesis_order),
    ).fetchone()
    if (hypothesis_order < 1 or int(prior["n"]) != hypothesis_order - 1
            or (hypothesis_order > 1 and (
                int(prior["first_order"]) != 1
                or int(prior["last_order"]) != hypothesis_order - 1
            ))):
        raise ValueError(f"production 前向 LOND 假设顺序不连续: {fid}")
    from quantqc.stats import online_lond_alpha
    expected_alpha = online_lond_alpha(
        hypothesis_order, int(prior["rejected"]),
        q=float(frozen["q"]), dependent=bool(frozen["dependent"]),
    )
    tolerance = max(1e-15, abs(expected_alpha) * 1e-12)
    expected_reject = bool(test["gate_passed"]) and test_p <= expected_alpha
    if (not 0.0 <= test_p <= 1.0
            or abs(alpha_i - expected_alpha) > tolerance
            or bool(test["online_reject"]) is not expected_reject
            or not expected_reject):
        raise ValueError(f"production 前向 LOND 拒绝语义不一致: {fid}")

    _, payload = _verified_project_file(
        evidence_path, evidence_sha256, label="production 前向证据",
    )
    expected_online = {
        key: test[key] for key in (
            "epoch", "hypothesis_order", "p_value", "alpha_i",
            "gate_passed", "online_reject", "data_key", "p_value_method",
            "evaluator_fingerprint",
        )
    }
    gate = payload.get("gate_evidence")
    window = payload.get("window")
    recipe_binding = payload.get("recipe")
    expected_recipe_path = str(
        (config.REGISTRY_DIR / "recipes" / f"{fid}.json").relative_to(
            config.PROJECT_ROOT,
        )
    )
    if (payload.get("schema_version") != 1
            or payload.get("factor_id") != fid
            or payload.get("research_epoch") != config.RESEARCH_EPOCH
            or payload.get("stage") != "forward_shadow"
            or payload.get("evaluator_fingerprint") != expected_evaluator
            or payload.get("tested_at") != test["tested_at"]
            or payload.get("data_key") != test["data_key"]
            or payload.get("online_fdr") != expected_online
            or not isinstance(window, dict)
            or not isinstance(recipe_binding, dict)
            or recipe_binding.get("path") != expected_recipe_path
            or recipe_binding.get("sha256") != row.get("sealed_recipe_sha256")
            or not isinstance(gate, dict)):
        raise ValueError(f"production 前向证据身份/冻结元数据无效: {fid}")

    try:
        start = pd.Timestamp(window["start"])
        end = pd.Timestamp(window["end"])
        sealed_end = pd.Timestamp(row["evaluator_sealed_data_end"])
        if isinstance(window["n_dates"], bool):
            raise ValueError
        n_dates = int(window["n_dates"])
        raw_dates = window["dates"]
        if not isinstance(raw_dates, list):
            raise ValueError
        window_dates = [pd.Timestamp(value) for value in raw_dates]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"production 前向窗口元数据无效: {fid}") from exc
    recipe = recipe if recipe is not None else _load_frozen_recipe(row)
    hold_days = recipe_execution_hold_days(recipe)
    required_dates = int(config.FORWARD_SHADOW_MIN_DAYS) + hold_days + 1
    if (start <= sealed_end or end < start or n_dates != required_dates
            or len(window_dates) != n_dates
            or not window_dates or window_dates[0] != start
            or window_dates[-1] != end
            or any(current <= previous for previous, current in zip(
                window_dates, window_dates[1:],
            ))):
        raise ValueError(
            "production 前向窗口未覆盖 seal 后完整标签期: "
            f"{fid} n_dates={n_dates} required={required_dates} "
            f"start={start.date()} sealed_end={sealed_end.date()}"
        )

    seal = {
        "sealed_at": row.get("sealed_at"),
        "sealed_data_end": row.get("sealed_data_end"),
        "recipe_sha256": row.get("sealed_recipe_sha256"),
    }
    evaluator_seal = {
        "sealed_at": row.get("evaluator_sealed_at"),
        "sealed_data_end": row.get("evaluator_sealed_data_end"),
        "evaluator_fingerprint": expected_evaluator,
    }
    research_input = gate.get("research_input")
    benchmark = (research_input or {}).get("benchmark_source")
    spec = gate.get("backtest_spec")
    if (gate.get("verdict") != "PASS"
            or gate.get("window_role") != "forward_shadow"
            or gate.get("research_epoch") != config.RESEARCH_EPOCH
            or gate.get("factor_seal") != seal
            or gate.get("evaluator_seal") != evaluator_seal
            or any(not value for value in evaluator_seal.values())
            or gate.get("online_fdr") != expected_online
            or gate.get("not_run")
            or gate.get("early_exit")
            or not isinstance(research_input, dict)
            or research_input.get("data_key") != test["data_key"]
            or not isinstance(benchmark, dict)
            or not benchmark.get("path")
            or not benchmark.get("sha256")
            or not benchmark.get("size")
            or not isinstance(spec, dict)):
        raise ValueError(f"production 前向 PASS/输入/seal 语义无效: {fid}")
    try:
        direction = 1.0 if float(row["is_ic_mean"]) >= 0 else -1.0
        spec_ok = (
            int(spec["hold"]) == hold_days
            and int(spec["q"]) == int(config.QUANTILE)
            and float(spec["direction"]) == direction
            and spec["window_start"] == str(start.date())
            and spec["window_end"] == str(end.date())
            and int(spec["n_positions"]) > 0
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"production 前向回测规格无效: {fid}") from exc
    if not spec_ok:
        raise ValueError(f"production 前向回测规格与冻结窗口不一致: {fid}")

    required_checks = (
        "net_annual>0", "beats_shanghai", "mdd>=-35%",
        "oos_ic_significant", "fold_pass_rate>=60%", "sensitivity>=60%",
    )
    checks = gate.get("checks")
    if not isinstance(checks, dict) or any(checks.get(key) is not True
                                           for key in required_checks):
        raise ValueError(f"production 前向必需 checks 未全通过: {fid}")
    full = gate.get("full")
    shanghai = gate.get("shanghai_bench")
    oos = gate.get("oos_ic")
    try:
        full_ok = (
            isinstance(full, dict) and float(full["annual_return"]) > 0
            and float(full["max_drawdown"]) >= -0.35
            and float(full["shanghai_excess_annual"]) > 0
            and isinstance(shanghai, dict) and shanghai.get("ok") is True
        )
        oos_ok = (
            isinstance(oos, dict)
            # legacy hold=10 时 131 个冻结交易日保证 120 个标签完成；
            # 实际窗口按 recipe hold 冻结，横截面缺失会让
            # 实际有效 IC 日少于 120，科学 gate 的冻结阈值仍是 60。
            and int(oos["n_dates"]) >= 60
            and int(oos["n_effective"]) > 0
            and float(oos["ic_mean"]) * direction > 0
            and abs(float(oos["ic_ir"])) >= 0.3
            and abs(float(oos["t_stat"])) >= 2.0
            and abs(float(oos["p_value"]) - test_p) <= 1e-12
            and oos["p_value_method"] == test["p_value_method"]
            and int(oos["p_value_hold"]) == hold_days
            and 0 <= int(oos["p_value_phase"]) < hold_days
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"production 前向经济性/OOS 证据无效: {fid}") from exc
    if not full_ok or not oos_ok:
        raise ValueError(f"production 前向 checks 与底层指标不一致: {fid}")

    folds = gate.get("folds")
    fold_pass = gate.get("fold_pass")
    if not isinstance(folds, list) or len(folds) != 5 or not isinstance(fold_pass, dict):
        raise ValueError(f"production 前向 folds 证据不完整: {fid}")
    fold_results = []
    fold_windows = []
    for fold in folds:
        try:
            declared_ok = fold.get("ok")
            if not isinstance(declared_ok, bool) or len(fold["val"]) != 2:
                raise ValueError
            val_start, val_end = (pd.Timestamp(value) for value in fold["val"])
            if not (start <= val_start <= val_end <= end):
                raise ValueError
            annual = fold.get("annual_return")
            excess = fold.get("shanghai_excess_annual")
            metrics_pass = (
                annual is not None and excess is not None
                and float(annual) > 0 and float(excess) > 0
            )
            if declared_ok is not metrics_pass:
                raise ValueError
            fold_results.append(declared_ok)
            fold_windows.append((val_start, val_end))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"production 前向 fold 语义无效: {fid}") from exc
    if (fold_windows[0][0] != start or fold_windows[-1][1] != end
            or any(current[0] <= previous[1]
                   for previous, current in zip(fold_windows, fold_windows[1:]))
            or len(set(fold_windows)) != len(fold_windows)):
        raise ValueError(f"production 前向 fold 窗口未唯一有序覆盖主窗口: {fid}")
    n_passed = sum(fold_results)
    expected_rate = round(n_passed / len(fold_results), 3)
    if (fold_pass.get("n_folds") != len(folds)
            or fold_pass.get("passed") != n_passed
            or float(fold_pass.get("pass_rate", -1)) != expected_rate
            or fold_pass.get("gate_ok") is not True
            or expected_rate < 0.6):
        raise ValueError(f"production 前向 fold 汇总语义无效: {fid}")

    sensitivity = gate.get("sensitivity")
    if not isinstance(sensitivity, list) or len(sensitivity) != 9:
        raise ValueError(f"production 前向 sensitivity 证据不完整: {fid}")
    from .gate import _sensitivity_specs
    expected_specs = {
        (int(item["hold"]), int(item["n_positions"]))
        for item in _sensitivity_specs(
            hold_days, {"n_positions": int(spec["n_positions"])}
        )
    }
    actual_specs = set()
    unique_paths = set()
    passed_paths = set()
    for item in sensitivity:
        try:
            pair = (int(item["hold"]), int(item["n_positions"]))
            declared_ok = item.get("ok")
            if not isinstance(declared_ok, bool):
                raise ValueError
            annual = item.get("annual_return")
            excess = item.get("shanghai_excess_annual")
            metrics_pass = (
                annual is not None and excess is not None
                and float(annual) > 0 and float(excess) > 0
            )
            fingerprint = item.get("execution_fingerprint")
            if (item.get("q") != int(config.QUANTILE)
                    or declared_ok is not metrics_pass
                    or (declared_ok and not fingerprint)):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"production 前向 sensitivity 语义无效: {fid}") from exc
        actual_specs.add(pair)
        if fingerprint:
            unique_paths.add(str(fingerprint))
            if declared_ok:
                passed_paths.add(str(fingerprint))
    sensitivity_ok = len(passed_paths) / len(sensitivity) >= 0.6
    if (actual_specs != expected_specs
            or gate.get("sensitivity_axes") != ["holding_days", "n_positions"]
            or gate.get("sensitivity_unique_paths") != len(unique_paths)
            or gate.get("sensitivity_duplicate_specs")
            != len(sensitivity) - len(unique_paths)
            or gate.get("sensitivity_pass") is not True
            or not sensitivity_ok):
        raise ValueError(f"production 前向 sensitivity 汇总语义无效: {fid}")
    return {
        "test": test,
        "test_epoch": dict(frozen),
        "payload": payload,
        "gate": gate,
        "hold_days": hold_days,
    }


_ADMISSION_MIN_SECURITIES = 30
_ADMISSION_MIN_RANK_CORRELATION = 0.999
_ADMISSION_MAX_ABS_DIFFERENCE = 1e-8


def _content_addressed_project_json(relative_path: str, sha256: str, *,
                                     directory: str, label: str
                                     ) -> tuple[Path, dict]:
    path, payload = _verified_project_file(relative_path, sha256, label=label)
    root = config.PROJECT_ROOT.resolve()
    relative = path.relative_to(root)
    if (relative.as_posix() != Path(str(relative_path)).as_posix()
            or not relative.as_posix().startswith(directory.rstrip("/") + "/")
            or path.name != f"{sha256}.json"
            or any(part.is_symlink() for part in [path, *path.parents]
                   if part != root)):
        raise ValueError(f"{label}不是项目内规范内容寻址普通文件")
    return path, payload


def _verified_blind_pass_evidence(
    c: sqlite3.Connection, row: dict, recipe: dict,
) -> tuple[dict, dict]:
    """Re-open and recompute every scientific binding behind a blind PASS."""
    fid = str(row.get("factor_id") or "")
    run = blind_run_for_candidate(c, fid)
    if not run or run.get("state") != "complete":
        raise ValueError("blind post-admission 缺少完整一次性盲测")
    authorization_check = source_gate.verify_blind_authorization(
        run.get("authorization_path"), run.get("authorization_sha256"),
        verify_panel=True,
    )
    authorization = authorization_check.get("authorization")
    try:
        candidate_binding = authorization["candidate"]
        chain_binding = authorization["chain"]
        implementation_files = authorization["implementation"]["files"]
        evaluator_binding = authorization["evaluator"]
        input_binding = authorization["input"]
    except (KeyError, TypeError) as exc:
        raise ValueError("blind post-admission authorization 结构无效") from exc
    implementation_identity = _json_sha256(_canonical_json({
        "files": implementation_files,
    }))
    if (authorization_check.get("valid") is not True
            or authorization_check.get("input_verified") is not True
            or candidate_binding.get("id") != fid
            or candidate_binding.get("research_record_sha256")
            != run.get("candidate_research_record_sha256")
            or chain_binding.get("implementation_audit_sha256")
            != run.get("implementation_audit_sha256")
            or implementation_identity
            != run.get("implementation_identity_sha256")
            or evaluator_binding.get("fingerprint")
            != run.get("evaluator_fingerprint")
            or input_binding.get("panel_cache_path") != run.get("panel_path")
            or input_binding.get("panel_cache_key") != run.get("panel_key")
            or input_binding.get("panel_cache_sha256")
            != run.get("panel_sha256")):
        raise ValueError("blind post-admission authorization 或输入绑定无效")
    evidence_path = str(run.get("result_evidence_path") or "")
    evidence_sha256 = str(run.get("result_evidence_sha256") or "")
    _, evidence = _content_addressed_project_json(
        evidence_path, evidence_sha256,
        directory=(
            "factor_system/registry/evidence/blind_runs/"
            f"{run['authorization_sha256']}"
        ),
        label="blind post-admission result evidence",
    )
    result = evidence.get("result")
    l3_fdr = evidence.get("l3_fdr")
    panel = evidence.get("panel")
    if (evidence.get("schema_version") != 1
            or evidence.get("stage") != "blind_discovery_validation_once"
            or evidence.get("candidate_id") != fid
            or evidence.get("authorization_path")
            != run.get("authorization_path")
            or evidence.get("authorization_sha256")
            != run.get("authorization_sha256")
            or evidence.get("candidate_research_record_sha256")
            != run.get("candidate_research_record_sha256")
            or evidence.get("implementation_identity_sha256")
            != run.get("implementation_identity_sha256")
            or evidence.get("implementation_audit_sha256")
            != run.get("implementation_audit_sha256")
            or evidence.get("evaluator_fingerprint")
            != run.get("evaluator_fingerprint")
            or evidence.get("final_status") != "validated_locked"
            or evidence.get("production_unlocked") is not False
            or not isinstance(result, dict)
            or result.get("passed") is not True
            or result.get("stage") != "L3"
            or not isinstance(l3_fdr, dict)
            or l3_fdr.get("reject") is not True
            or not isinstance(panel, dict)
            or panel.get("path") != run.get("panel_path")
            or panel.get("key") != run.get("panel_key")
            or panel.get("sha256") != run.get("panel_sha256")
            or panel.get("data_key") != input_binding.get("panel_data_key")):
        raise ValueError("blind post-admission 结果身份或科学判定无效")
    if (row.get("research_epoch") != config.RESEARCH_EPOCH
            or row.get("research_record_sha256")
            != run.get("candidate_research_record_sha256")
            or not row.get("sealed_recipe_sha256")
            or recipe.get("research_record_sha256")
            != row.get("research_record_sha256")):
        raise ValueError("blind post-admission factor seal 身份无效")
    verify_blind_built_source_record(row, recipe)
    evidence_recipe = evidence.get("recipe")
    for key in ("id", "kind", "params", "execution_spec", "validation_spec"):
        if not isinstance(evidence_recipe, dict) or (
            evidence_recipe.get(key) != recipe.get(key)
        ):
            raise ValueError("blind post-admission recipe 与盲测证据不一致")
    return run, evidence


def _blind_post_admission_state(
    c: sqlite3.Connection, fid: str,
) -> tuple[dict, dict, dict, dict]:
    """Recompute the sole validated-blind admission from immutable evidence."""
    raw = c.execute(
        "SELECT * FROM factors WHERE factor_id=?", (str(fid),),
    ).fetchone()
    if raw is None:
        raise ValueError(f"factor 不存在: {fid}")
    row = dict(raw)
    if row.get("status") != "validated_locked":
        raise ValueError("blind post-admission 只接受 validated_locked")
    recipe = _load_frozen_recipe(row)
    run, evidence = _verified_blind_pass_evidence(c, row, recipe)
    return row, run, evidence, recipe


def _write_blind_post_admission(payload: dict) -> tuple[str, str, str]:
    body = _canonical_json(payload) + "\n"
    sha256 = _json_sha256(body)
    directory = config.REGISTRY_DIR / "blind_post_admissions"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{sha256}.json"
    if path.exists():
        if not path.is_file() or path.read_bytes() != body.encode("utf-8"):
            raise RuntimeError("blind post-admission 内容寻址冲突")
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(body.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        if not path.is_file() or path.read_bytes() != body.encode("utf-8"):
            raise RuntimeError("blind post-admission 原子发布失败")
    return str(path.relative_to(config.PROJECT_ROOT)), sha256, body


def _verified_blind_post_admission_record(
    c: sqlite3.Connection, row: dict, recipe: dict,
) -> dict:
    """Verify the physical post-blind artifact before normal lifecycle use."""
    fid = str(row.get("factor_id") or "")
    raw = c.execute(
        "SELECT * FROM blind_post_admissions WHERE candidate_id=?", (fid,),
    ).fetchone()
    if raw is None:
        raise ValueError("blind post-admission 缺失")
    admission = dict(raw)
    _, payload = _content_addressed_project_json(
        str(admission["admission_path"]), str(admission["admission_sha256"]),
        directory="factor_system/registry/blind_post_admissions",
        label="blind post-admission",
    )
    try:
        stored_payload = json.loads(str(admission["artifact_json"]))
    except json.JSONDecodeError as exc:
        raise ValueError("blind post-admission DB 制品损坏") from exc
    run, evidence = _verified_blind_pass_evidence(c, row, recipe)
    bindings = payload.get("bindings") if isinstance(payload, dict) else None
    decision = payload.get("decision") if isinstance(payload, dict) else None
    authorization = (
        bindings.get("authorization") if isinstance(bindings, dict) else None
    )
    result_evidence = (
        bindings.get("result_evidence") if isinstance(bindings, dict) else None
    )
    if (payload != stored_payload
            or payload.get("schema_version") != 1
            or payload.get("artifact_type")
            != "blind_post_validation_admission"
            or payload.get("candidate_id") != fid
            or payload.get("research_epoch") != config.RESEARCH_EPOCH
            or not isinstance(bindings, dict)
            or not isinstance(authorization, dict)
            or authorization != {
                "path": run["authorization_path"],
                "sha256": admission["authorization_sha256"],
            }
            or result_evidence != {
                "path": admission["result_evidence_path"],
                "sha256": admission["result_evidence_sha256"],
            }
            or bindings.get("panel") != evidence.get("panel")
            or bindings.get("research_record_sha256")
            != row.get("research_record_sha256")
            or bindings.get("sealed_recipe_sha256")
            != row.get("sealed_recipe_sha256")
            or bindings.get("evaluator_fingerprint")
            != admission["evaluator_fingerprint"]
            or not isinstance(decision, dict)
            or decision.get("blind_final_status") != "validated_locked"
            or decision.get("l3_passed") is not True
            or decision.get("fdr_reject") is not True
            or decision.get("production_unlocked") is not False
            or decision.get("next_status") != "registered"
            or run.get("authorization_sha256")
            != admission["authorization_sha256"]
            or run.get("result_evidence_path")
            != admission["result_evidence_path"]
            or run.get("result_evidence_sha256")
            != admission["result_evidence_sha256"]
            or run.get("evaluator_fingerprint")
            != admission["evaluator_fingerprint"]
            or run.get("panel_sha256") != admission["panel_sha256"]
            or admission["candidate_research_record_sha256"]
            != row.get("research_record_sha256")
            or admission["sealed_recipe_sha256"]
            != row.get("sealed_recipe_sha256")
            or recipe.get("research_record_sha256")
            != row.get("research_record_sha256")):
        raise ValueError("blind post-admission 身份或证据无效")
    return admission


def verify_factor_source_for_pipeline(
    c: sqlite3.Connection, row, recipe: dict,
) -> dict:
    """Accept a normal runnable source or an exactly post-admitted blind PASS."""
    try:
        return verify_frozen_source_record(row, recipe)
    except ValueError as ordinary_error:
        try:
            result = verify_blind_built_source_record(row, recipe)
        except ValueError:
            raise ordinary_error
        _verified_blind_post_admission_record(c, dict(row), recipe)
        return {**result, "post_blind_admitted": True}


def admit_blind_validated_factor(
    c: sqlite3.Connection, fid: str,
) -> dict:
    """Admit one blind PASS to historical confirmation, never to production."""
    if c.in_transaction:
        raise RuntimeError("blind post-admission 要求独立事务")
    existing = c.execute(
        "SELECT * FROM blind_post_admissions WHERE candidate_id=?", (str(fid),),
    ).fetchone()
    if existing is not None:
        row = c.execute(
            "SELECT * FROM factors WHERE factor_id=?", (str(fid),),
        ).fetchone()
        if row is None or row["status"] != "registered":
            raise RuntimeError("blind post-admission 已存在但 factor 状态漂移")
        row_dict = dict(row)
        return _verified_blind_post_admission_record(
            c, row_dict, _load_frozen_recipe(row_dict),
        )

    c.execute("BEGIN IMMEDIATE")
    try:
        row, run, evidence, recipe = _blind_post_admission_state(c, str(fid))
        payload = {
            "schema_version": 1,
            "artifact_type": "blind_post_validation_admission",
            "candidate_id": str(fid),
            "research_epoch": config.RESEARCH_EPOCH,
            "bindings": {
                "authorization": {
                    "path": run["authorization_path"],
                    "sha256": run["authorization_sha256"],
                },
                "result_evidence": {
                    "path": run["result_evidence_path"],
                    "sha256": run["result_evidence_sha256"],
                },
                "research_record_sha256": row["research_record_sha256"],
                "sealed_recipe_sha256": row["sealed_recipe_sha256"],
                "evaluator_fingerprint": run["evaluator_fingerprint"],
                "panel": evidence["panel"],
            },
            "decision": {
                "blind_final_status": "validated_locked",
                "l3_passed": True,
                "fdr_reject": True,
                "production_unlocked": False,
                "next_status": "registered",
                "next_stage": "historical_confirmation",
            },
        }
        path, sha256, body = _write_blind_post_admission(payload)
        c.execute(
            """INSERT INTO blind_post_admissions
                 (admission_sha256,admission_path,candidate_id,
                  authorization_sha256,result_evidence_path,
                  result_evidence_sha256,candidate_research_record_sha256,
                  sealed_recipe_sha256,evaluator_fingerprint,panel_sha256,
                  artifact_json,admitted_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                sha256, path, str(fid), run["authorization_sha256"],
                run["result_evidence_path"], run["result_evidence_sha256"],
                row["research_record_sha256"], row["sealed_recipe_sha256"],
                run["evaluator_fingerprint"], run["panel_sha256"], body,
                _now(),
            ),
        )
        changed = c.execute(
            "UPDATE factors SET status='registered',status_reason=?,updated_at=? "
            "WHERE factor_id=? AND status='validated_locked'",
            (
                "一次性盲测与 FDR 已通过；进入独立历史确认，尚未投产",
                _now(), str(fid),
            ),
        ).rowcount
        if changed != 1:
            raise RuntimeError("blind post-admission 状态转移失败")
        _append_factor_trial_snapshot(
            c, str(fid), event="blind_post_validation_admitted",
            details={"admission_path": path, "admission_sha256": sha256},
        )
        c.commit()
        return dict(c.execute(
            "SELECT * FROM blind_post_admissions WHERE candidate_id=?",
            (str(fid),),
        ).fetchone())
    except Exception:
        c.rollback()
        raise


def _verified_independent_verifier(binding: dict) -> tuple[Path, str]:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise ValueError("production 独立 verifier 绑定格式无效")
    root = config.PROJECT_ROOT.resolve()
    path = (root / str(binding["path"])).resolve()
    try:
        relative = path.relative_to(root)
        raw = path.read_bytes()
    except (OSError, ValueError) as exc:
        raise ValueError("production 独立 verifier 源码缺失或越界") from exc
    if (relative.as_posix() != Path(str(binding["path"])).as_posix()
            or not relative.as_posix().startswith("research/independent_verifiers/")
            or path.suffix != ".py"
            or any(part.is_symlink() for part in [path, *path.parents]
                   if part != root)
            or hashlib.sha256(raw).hexdigest() != binding["sha256"]):
        raise ValueError("production 独立 verifier 源码路径/hash 无效")
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise ValueError("production 独立 verifier 源码无法静态审计") from exc
    forbidden_calls = {
        "rebuild_factor", "build_atom_frames", "eval", "exec", "__import__",
        "import_module", "system", "popen", "run_module", "run_path",
        "getattr", "compile", "open", "input",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        else:
            modules = []
        if any(module == "factor_system" or module.startswith("factor_system.")
               for module in modules):
            raise ValueError("production 独立 verifier 不得导入 factor_system evaluator")
        if any(module.split(".", 1)[0] in {"subprocess", "runpy", "importlib"}
               for module in modules):
            raise ValueError("production 独立 verifier 不得使用子进程/动态导入旁路")
        if any(module.split(".", 1)[0] == "os" for module in modules):
            raise ValueError("production 独立 verifier 调用了禁用 evaluator/动态执行")
        if modules:
            raise ValueError("production 独立 verifier 必须是无导入的纯函数")
        if isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ValueError("production 独立 verifier 不得访问双下划线属性")
        if isinstance(node, ast.Call):
            name = (node.func.id if isinstance(node.func, ast.Name)
                    else node.func.attr if isinstance(node.func, ast.Attribute)
                    else "")
            if name in forbidden_calls:
                raise ValueError("production 独立 verifier 调用了禁用 evaluator/动态执行")
    top_level = [
        node for node in tree.body
        if not (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )
    ]
    if (len(top_level) != 1
            or not isinstance(top_level[0], ast.FunctionDef)
            or top_level[0].name != "reproduce"
            or top_level[0].decorator_list
            or len(top_level[0].args.args) != 1
            or top_level[0].args.vararg is not None
            or top_level[0].args.kwarg is not None):
        raise ValueError("production 独立 verifier 必须只定义 reproduce(payload)")
    return path, raw.decode("utf-8")


_INDEPENDENT_RUNNER_SOURCE = """
import json
import sys
envelope = json.load(sys.stdin)
safe_builtins = {
    'abs': abs, 'all': all, 'any': any, 'bool': bool, 'dict': dict,
    'enumerate': enumerate, 'float': float, 'int': int,
    'isinstance': isinstance, 'len': len, 'list': list, 'max': max,
    'min': min, 'range': range, 'round': round, 'sorted': sorted,
    'sum': sum, 'tuple': tuple, 'ValueError': ValueError, 'zip': zip,
}
namespace = {'__builtins__': safe_builtins}
exec(compile(envelope['source'], 'independent_verifier.py', 'exec'), namespace)
result = namespace['reproduce'](envelope['input'])
json.dump(result, sys.stdout, sort_keys=True, separators=(',', ':'), allow_nan=False)
""".strip()


def _verified_independent_input(
    binding: dict, *, row: dict, verified_forward: dict,
    expected_securities: list[str],
) -> dict:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise ValueError("production independent raw input 绑定无效")
    _, payload = _content_addressed_project_json(
        binding["path"], binding["sha256"],
        directory="factor_system/registry/production_reproduction_inputs",
        label="production independent raw input",
    )
    test = verified_forward["test"]
    expected_dates = verified_forward["payload"]["window"]["dates"][
        :int(config.FORWARD_SHADOW_MIN_DAYS)
    ]
    if (set(payload) != {
            "schema_version", "input_type", "factor_id", "research_epoch",
            "recipe_sha256", "data_key", "producer", "signal_dates",
            "history_dates", "securities", "frames",
        }
            or payload.get("schema_version") != 1
            or payload.get("input_type") != "independent_factor_raw_input"
            or payload.get("factor_id") != row.get("factor_id")
            or payload.get("research_epoch") != config.RESEARCH_EPOCH
            or payload.get("recipe_sha256") != row.get("sealed_recipe_sha256")
            or payload.get("data_key") != test["data_key"]
            or payload.get("producer") != {
                "type": "primary_input_freezer",
                "evaluator_fingerprint": test["evaluator_fingerprint"],
            }
            or payload.get("signal_dates") != expected_dates
            or payload.get("securities") != expected_securities):
        raise ValueError("production independent raw input 身份无效")
    history_dates = payload.get("history_dates")
    frames = payload.get("frames")
    if (not isinstance(history_dates, list) or not history_dates
            or len(set(history_dates)) != len(history_dates)
            or history_dates != sorted(history_dates)
            or history_dates[-1] != expected_dates[-1]
            or not isinstance(frames, dict) or not frames):
        raise ValueError("production independent raw input 日期/字段无效")
    for name, matrix in frames.items():
        if (not isinstance(name, str) or not name
                or not isinstance(matrix, list)
                or len(matrix) != len(history_dates)
                or any(
                    not isinstance(values, list)
                    or len(values) != len(expected_securities)
                    for values in matrix
                )):
            raise ValueError("production independent raw input 矩阵无效")
    return payload


def _controlled_independent_output(
    verifier_binding: dict, input_payload: dict, input_binding: dict,
) -> tuple[list[list[float]], dict]:
    _, source = _verified_independent_verifier(verifier_binding)
    envelope = json.dumps(
        {"source": source, "input": input_payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-c", _INDEPENDENT_RUNNER_SOURCE],
            input=envelope, text=True, capture_output=True, timeout=60,
            check=False, env={"PYTHONHASHSEED": "0", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError("production 独立 verifier 受控执行失败") from exc
    if completed.returncode != 0 or completed.stderr:
        raise ValueError("production 独立 verifier 受控执行未干净完成")
    try:
        output = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("production 独立 verifier 输出不是严格 JSON") from exc
    if not isinstance(output, list):
        raise ValueError("production 独立 verifier 输出矩阵无效")
    matrix: list[list[float]] = []
    for raw_row in output:
        if not isinstance(raw_row, list):
            raise ValueError("production 独立 verifier 输出矩阵无效")
        values = []
        for raw in raw_row:
            if (isinstance(raw, bool) or not isinstance(raw, (int, float))
                    or not math.isfinite(float(raw))):
                raise ValueError("production 独立 verifier 输出含非有限值")
            values.append(float(raw))
        matrix.append(values)
    output_body = json.dumps(
        matrix, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    attestation = {
        "runner": "restricted_python_pure_function_v1",
        "runner_sha256": hashlib.sha256(
            _INDEPENDENT_RUNNER_SOURCE.encode("utf-8")
        ).hexdigest(),
        "python": sys.version.split()[0],
        "verifier_source": verifier_binding,
        "independent_input": input_binding,
        "output_sha256": hashlib.sha256(output_body.encode("utf-8")).hexdigest(),
        "exit_code": 0,
        "stderr_empty": True,
    }
    return matrix, attestation


def _verified_signal_sample(binding: dict, *, role: str, row: dict,
                            verified_forward: dict, producer: dict
                            ) -> tuple[list[str], list[list[float]]]:
    if not isinstance(binding, dict) or set(binding) != {"path", "sha256"}:
        raise ValueError(f"production {role} signal sample 绑定无效")
    directory = (
        "factor_system/registry/production_signal_samples/primary"
        if role == "primary"
        else "reports/factor_signal_samples/independent"
    )
    _, sample = _content_addressed_project_json(
        binding["path"], binding["sha256"], directory=directory,
        label=f"production {role} signal sample",
    )
    expected_dates = verified_forward["payload"]["window"]["dates"][
        :int(config.FORWARD_SHADOW_MIN_DAYS)
    ]
    if (set(sample) != {
            "schema_version", "sample_type", "factor_id", "research_epoch",
            "recipe_sha256", "data_key", "producer", "dates", "securities",
            "values",
        }
            or sample.get("schema_version") != 1
            or sample.get("sample_type") != f"{role}_factor_signal_sample"
            or sample.get("factor_id") != row.get("factor_id")
            or sample.get("research_epoch") != config.RESEARCH_EPOCH
            or sample.get("recipe_sha256") != row.get("sealed_recipe_sha256")
            or sample.get("data_key") != verified_forward["test"]["data_key"]
            or sample.get("producer") != producer
            or sample.get("dates") != expected_dates):
        raise ValueError(f"production {role} signal sample 身份/日期轴无效")
    securities = sample.get("securities")
    values = sample.get("values")
    if (not isinstance(securities, list) or len(securities) < 30
            or any(not isinstance(code, str) or not code for code in securities)
            or len(set(securities)) != len(securities)
            or not isinstance(values, list) or len(values) != len(expected_dates)):
        raise ValueError(f"production {role} signal sample 证券轴/矩阵无效")
    matrix = []
    for raw_row in values:
        if not isinstance(raw_row, list) or len(raw_row) != len(securities):
            raise ValueError(f"production {role} signal sample 矩阵维度无效")
        if any(isinstance(value, bool) or not isinstance(value, (int, float))
               or not math.isfinite(float(value)) for value in raw_row):
            raise ValueError(f"production {role} signal sample 含缺值/非有限值")
        matrix.append([float(value) for value in raw_row])
    return securities, matrix


def _midranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    start = 0
    while start < len(order):
        end = start + 1
        while end < len(order) and values[order[end]] == values[order[start]]:
            end += 1
        rank = (start + 1 + end) / 2.0
        for index in order[start:end]:
            ranks[index] = rank
        start = end
    return ranks


def _signal_sample_comparison(primary: list[list[float]],
                              independent: list[list[float]]) -> tuple[float, float]:
    correlations = []
    maximum_difference = 0.0
    for left, right in zip(primary, independent):
        maximum_difference = max(
            maximum_difference,
            max(abs(a - b) for a, b in zip(left, right)),
        )
        left_rank, right_rank = _midranks(left), _midranks(right)
        left_mean = sum(left_rank) / len(left_rank)
        right_mean = sum(right_rank) / len(right_rank)
        numerator = sum(
            (a - left_mean) * (b - right_mean)
            for a, b in zip(left_rank, right_rank)
        )
        left_ss = sum((value - left_mean) ** 2 for value in left_rank)
        right_ss = sum((value - right_mean) ** 2 for value in right_rank)
        denominator = math.sqrt(left_ss * right_ss)
        if denominator <= 0:
            raise ValueError("production signal sample 存在无横截面差异日期")
        correlations.append(numerator / denominator)
    if len(correlations) != int(config.FORWARD_SHADOW_MIN_DAYS):
        raise ValueError("production signal sample 未覆盖完整信号日")
    return sum(correlations) / len(correlations), maximum_difference


def verify_production_admission_certificate(
    c: sqlite3.Connection,
    row: dict,
    recipe: dict,
    verified_forward: dict,
    *,
    certificate_path: str,
    certificate_sha256: str,
) -> dict:
    """Validate a separate independent-reproduction certificate, never booleans."""
    fid = str(row["factor_id"])
    _, certificate = _content_addressed_project_json(
        certificate_path, certificate_sha256,
        directory="factor_system/registry/production_admissions",
        label="production admission certificate",
    )
    if set(certificate) != {
        "schema_version", "certificate_type", "factor_id", "research_epoch",
        "bindings", "policy",
    }:
        raise ValueError("production admission certificate schema 无效")
    bindings = certificate.get("bindings")
    if not isinstance(bindings, dict) or set(bindings) != {
        "research_record_sha256", "recipe_sha256", "forward_evidence",
        "independent_reproduction", "verifier_source",
        "verifier_commitment",
    }:
        raise ValueError("production admission certificate bindings 无效")
    reproduction_binding = bindings["independent_reproduction"]
    if not isinstance(reproduction_binding, dict) or set(reproduction_binding) != {
        "path", "sha256",
    }:
        raise ValueError("production 独立复现制品绑定无效")
    _, reproduction = _content_addressed_project_json(
        reproduction_binding["path"], reproduction_binding["sha256"],
        directory="reports/factor_independent_reproduction",
        label="production 独立复现制品",
    )
    verifier_binding = bindings["verifier_source"]
    if verifier_binding != reproduction.get("verifier_source"):
        raise ValueError("production 独立复现制品与 verifier 绑定不一致")
    _verified_independent_verifier(verifier_binding)
    commitment = _verified_independent_verifier_commitment(c, row)
    if (bindings["verifier_commitment"] != {
            "path": commitment["path"], "sha256": commitment["sha256"],
            }
            or reproduction.get("verifier_commitment")
            != bindings["verifier_commitment"]
            or commitment["verifier_source"] != verifier_binding):
        raise ValueError("production 独立 verifier 未使用前向前冻结承诺")

    test = verified_forward["test"]
    window = verified_forward["payload"]["window"]
    hold_days = int(verified_forward["hold_days"])
    expected_forward = {
        "test_epoch": str(test["epoch"]),
        "path": str(test["evidence_path"]),
        "sha256": str(test["evidence_sha256"]),
        "data_key": str(test["data_key"]),
        "evaluator_fingerprint": str(test["evaluator_fingerprint"]),
    }
    expected_window = {
        "start": window["start"], "end": window["end"],
        "n_dates": int(window["n_dates"]),
    }
    if (set(reproduction) != {
            "schema_version", "artifact_type", "factor_id", "research_epoch",
            "research_record_sha256", "recipe_sha256", "execution_spec",
            "forward_evidence", "window", "verifier_source", "signal_samples",
            "verifier_commitment", "independent_input", "runner_attestation",
            "comparison",
        }
            or reproduction.get("schema_version") != 1
            or reproduction.get("artifact_type")
            != "independent_factor_reproduction"
            or reproduction.get("factor_id") != fid
            or reproduction.get("research_epoch") != config.RESEARCH_EPOCH
            or reproduction.get("research_record_sha256")
            != row.get("research_record_sha256")
            or reproduction.get("recipe_sha256")
            != row.get("sealed_recipe_sha256")
            or reproduction.get("execution_spec") != {"hold_days": hold_days}
            or reproduction.get("forward_evidence") != expected_forward
            or reproduction.get("window") != expected_window):
        raise ValueError("production 独立复现制品身份/冻结输入无效")
    signal_samples = reproduction.get("signal_samples")
    primary_binding = verified_forward["gate"].get("primary_signal_sample")
    if (not isinstance(primary_binding, dict)
            or not isinstance(signal_samples, dict)
            or set(signal_samples) != {"primary", "independent"}
            or signal_samples.get("primary") != primary_binding):
        raise ValueError("production 独立复现未绑定冻结 primary signal sample")
    primary_securities, primary_matrix = _verified_signal_sample(
        signal_samples["primary"], role="primary", row=row,
        verified_forward=verified_forward,
        producer={
            "type": "primary_evaluator",
            "evaluator_fingerprint": test["evaluator_fingerprint"],
        },
    )
    independent_securities, independent_matrix = _verified_signal_sample(
        signal_samples["independent"], role="independent", row=row,
        verified_forward=verified_forward,
        producer={"type": "independent_verifier", **verifier_binding},
    )
    if independent_securities != primary_securities:
        raise ValueError("production primary/independent signal sample 证券轴不一致")
    input_binding = verified_forward["gate"].get(
        "independent_reproduction_input"
    )
    if (not isinstance(input_binding, dict)
            or reproduction.get("independent_input") != input_binding):
        raise ValueError("production 独立复现未绑定冻结 raw input")
    input_payload = _verified_independent_input(
        input_binding, row=row, verified_forward=verified_forward,
        expected_securities=primary_securities,
    )
    controlled_matrix, controlled_attestation = _controlled_independent_output(
        verifier_binding, input_payload, input_binding,
    )
    if (controlled_matrix != independent_matrix
            or reproduction.get("runner_attestation")
            != controlled_attestation):
        raise ValueError("production 独立 sample 不是受控 verifier 输出")
    computed_rank_correlation, computed_max_abs_difference = (
        _signal_sample_comparison(primary_matrix, independent_matrix)
    )
    comparison = reproduction.get("comparison")
    if not isinstance(comparison, dict) or set(comparison) != {
        "n_dates", "n_securities", "rank_correlation", "max_abs_difference",
    }:
        raise ValueError("production 独立复现 comparison schema 无效")
    values = (
        comparison["n_dates"], comparison["n_securities"],
        comparison["rank_correlation"], comparison["max_abs_difference"],
    )
    if (any(isinstance(value, bool) for value in values)
            or not all(isinstance(value, int) for value in values[:2])
            or not all(isinstance(value, (int, float)) for value in values[2:])):
        raise ValueError("production 独立复现 comparison 数值无效")
    n_dates, n_securities = values[:2]
    rank_correlation, max_abs_difference = map(float, values[2:])
    if (n_dates != int(config.FORWARD_SHADOW_MIN_DAYS)
            or n_securities != len(primary_securities)
            or not math.isfinite(rank_correlation)
            or abs(rank_correlation - computed_rank_correlation) > 1e-12
            or rank_correlation < _ADMISSION_MIN_RANK_CORRELATION
            or not math.isfinite(max_abs_difference)
            or abs(max_abs_difference - computed_max_abs_difference) > 1e-12
            or max_abs_difference < 0
            or max_abs_difference > _ADMISSION_MAX_ABS_DIFFERENCE):
        raise ValueError("production 独立复现 comparison 未达到准入阈值")

    expected_policy = {
        "required_forward_dates": (
            int(config.FORWARD_SHADOW_MIN_DAYS) + hold_days + 1
        ),
        "completed_signal_dates": int(config.FORWARD_SHADOW_MIN_DAYS),
        "hold_days": hold_days,
        "minimum_securities": _ADMISSION_MIN_SECURITIES,
        "minimum_rank_correlation": _ADMISSION_MIN_RANK_CORRELATION,
        "maximum_abs_difference": _ADMISSION_MAX_ABS_DIFFERENCE,
    }
    if (certificate.get("schema_version") != 1
            or certificate.get("certificate_type")
            != "independent_production_admission"
            or certificate.get("factor_id") != fid
            or certificate.get("research_epoch") != config.RESEARCH_EPOCH
            or bindings["research_record_sha256"]
            != row.get("research_record_sha256")
            or bindings["recipe_sha256"] != row.get("sealed_recipe_sha256")
            or bindings["forward_evidence"] != expected_forward
            or certificate.get("policy") != expected_policy):
        raise ValueError("production admission certificate 身份/政策无效")
    return certificate


def _write_content_addressed_payload(
    directory: str, payload: dict,
) -> tuple[str, str]:
    body = (_canonical_json(payload) + "\n").encode("utf-8")
    sha256 = hashlib.sha256(body).hexdigest()
    target_dir = config.PROJECT_ROOT / directory
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{sha256}.json"
    if path.exists():
        if not path.is_file() or path.read_bytes() != body:
            raise RuntimeError("production 内容寻址制品冲突")
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        if not path.is_file() or path.read_bytes() != body:
            raise RuntimeError("production 内容寻址制品原子发布失败")
    return str(path.relative_to(config.PROJECT_ROOT)), sha256


def _verified_independent_verifier_commitment(
    c: sqlite3.Connection, row: dict,
) -> dict:
    fid = str(row.get("factor_id") or "")
    raw = c.execute(
        "SELECT * FROM independent_verifier_commitments WHERE candidate_id=?",
        (fid,),
    ).fetchone()
    if raw is None:
        raise ValueError("production 独立 verifier 未在前向观察前冻结")
    commitment = dict(raw)
    _, payload = _content_addressed_project_json(
        commitment["commitment_path"], commitment["commitment_sha256"],
        directory="factor_system/registry/independent_verifier_commitments",
        label="production independent verifier commitment",
    )
    try:
        stored_payload = json.loads(commitment["artifact_json"])
    except json.JSONDecodeError as exc:
        raise ValueError("production verifier commitment DB 制品损坏") from exc
    verifier = {
        "path": commitment["verifier_path"],
        "sha256": commitment["verifier_sha256"],
    }
    if (payload != stored_payload
            or set(payload) != {
                "schema_version", "artifact_type", "factor_id",
                "research_epoch", "research_record_sha256",
                "recipe_sha256", "evaluator_fingerprint",
                "verifier_source", "policy",
            }
            or payload.get("schema_version") != 1
            or payload.get("artifact_type")
            != "independent_verifier_precommitment"
            or payload.get("factor_id") != fid
            or payload.get("research_epoch") != config.RESEARCH_EPOCH
            or payload.get("research_record_sha256")
            != row.get("research_record_sha256")
            or payload.get("recipe_sha256")
            != row.get("sealed_recipe_sha256")
            or payload.get("evaluator_fingerprint")
            != row.get("evaluator_fingerprint")
            or payload.get("verifier_source") != verifier
            or payload.get("policy") != {
                "committed_before_forward_observation": True,
                "replacement_allowed": False,
            }):
        raise ValueError("production verifier commitment 身份无效")
    _verified_independent_verifier(verifier)
    return {
        "path": commitment["commitment_path"],
        "sha256": commitment["commitment_sha256"],
        "verifier_source": verifier,
    }


def require_independent_verifier_commitment(
    c: sqlite3.Connection, row,
) -> dict:
    """Public fail-closed check used before any forward observation."""
    return _verified_independent_verifier_commitment(c, dict(row))


def commit_independent_verifier(
    c: sqlite3.Connection, fid: str, *, path: str, sha256: str,
) -> dict:
    """Freeze the clean-room verifier before any forward observation exists."""
    if c.in_transaction:
        raise RuntimeError("production verifier commitment 要求独立事务")
    existing = c.execute(
        "SELECT * FROM independent_verifier_commitments WHERE candidate_id=?",
        (str(fid),),
    ).fetchone()
    if existing is not None:
        row = c.execute(
            "SELECT * FROM factors WHERE factor_id=?", (str(fid),),
        ).fetchone()
        if row is None:
            raise ValueError(f"factor 不存在: {fid}")
        verified = _verified_independent_verifier_commitment(c, dict(row))
        if verified["verifier_source"] != {"path": path, "sha256": sha256}:
            raise ValueError("production verifier commitment 已冻结，不得替换")
        return dict(existing)

    c.execute("BEGIN IMMEDIATE")
    try:
        raw = c.execute(
            "SELECT * FROM factors WHERE factor_id=?", (str(fid),),
        ).fetchone()
        if raw is None:
            raise ValueError(f"factor 不存在: {fid}")
        row = dict(raw)
        if row.get("status") not in {
            "registered", "backtesting", "historical_pass",
        }:
            raise ValueError("production verifier 只能在历史/前向阶段前冻结")
        recipe = _load_frozen_recipe(row)
        verify_factor_source_for_pipeline(c, row, recipe)
        from .production import forward_test_epoch
        forward_exists = c.execute(
            "SELECT 1 FROM factor_tests WHERE factor_id=? AND epoch=? LIMIT 1",
            (str(fid), forward_test_epoch()),
        ).fetchone()
        if forward_exists is not None:
            raise ValueError("production verifier 必须在前向观察前冻结")
        verifier = {"path": path, "sha256": sha256}
        _verified_independent_verifier(verifier)
        payload = {
            "schema_version": 1,
            "artifact_type": "independent_verifier_precommitment",
            "factor_id": str(fid),
            "research_epoch": config.RESEARCH_EPOCH,
            "research_record_sha256": row["research_record_sha256"],
            "recipe_sha256": row["sealed_recipe_sha256"],
            "evaluator_fingerprint": row["evaluator_fingerprint"],
            "verifier_source": verifier,
            "policy": {
                "committed_before_forward_observation": True,
                "replacement_allowed": False,
            },
        }
        commitment_path, commitment_sha256 = _write_content_addressed_payload(
            "factor_system/registry/independent_verifier_commitments", payload,
        )
        body = _canonical_json(payload)
        c.execute(
            """INSERT INTO independent_verifier_commitments
                 (candidate_id,commitment_sha256,commitment_path,
                  verifier_path,verifier_sha256,
                  candidate_research_record_sha256,sealed_recipe_sha256,
                  evaluator_fingerprint,artifact_json,committed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                str(fid), commitment_sha256, commitment_path, path, sha256,
                row["research_record_sha256"], row["sealed_recipe_sha256"],
                row["evaluator_fingerprint"], body, _now(),
            ),
        )
        c.commit()
        return dict(c.execute(
            "SELECT * FROM independent_verifier_commitments WHERE candidate_id=?",
            (str(fid),),
        ).fetchone())
    except Exception:
        c.rollback()
        raise


def create_and_attach_production_admission(
    c: sqlite3.Connection, fid: str, *,
    verifier_path: str, verifier_sha256: str,
) -> dict:
    """Run the frozen clean-room verifier; callers cannot submit its sample."""
    raw = c.execute("SELECT * FROM factors WHERE factor_id=?", (fid,)).fetchone()
    if raw is None:
        raise ValueError(f"factor 不存在: {fid}")
    row = dict(raw)
    if row.get("status") not in {"historical_pass", "backtesting"}:
        raise ValueError("production admission 只能在完整前向检验后生成")
    recipe = _load_frozen_recipe(row)
    verify_factor_source_for_pipeline(c, row, recipe)
    verified = verify_forward_production_semantics(
        c, row, test_epoch=row.get("backtest_epoch"),
        evidence_path=row.get("evidence_path"),
        evidence_sha256=row.get("evidence_sha256"), recipe=recipe,
    )
    verifier_binding = {"path": verifier_path, "sha256": verifier_sha256}
    commitment = _verified_independent_verifier_commitment(c, row)
    if commitment["verifier_source"] != verifier_binding:
        raise ValueError("production verifier 与前向前冻结承诺不一致")
    primary_binding = verified["gate"].get("primary_signal_sample")
    if not isinstance(primary_binding, dict):
        raise ValueError("production 缺少冻结 primary signal sample")
    primary_securities, primary_matrix = _verified_signal_sample(
        primary_binding, role="primary", row=row,
        verified_forward=verified,
        producer={
            "type": "primary_evaluator",
            "evaluator_fingerprint": verified["test"]["evaluator_fingerprint"],
        },
    )
    input_binding = verified["gate"].get("independent_reproduction_input")
    input_payload = _verified_independent_input(
        input_binding, row=row, verified_forward=verified,
        expected_securities=primary_securities,
    )
    independent_matrix, attestation = _controlled_independent_output(
        verifier_binding, input_payload, input_binding,
    )
    if (len(independent_matrix) != len(primary_matrix)
            or any(len(values) != len(primary_securities)
                   for values in independent_matrix)):
        raise ValueError("production 独立 verifier 输出维度无效")
    independent_sample = {
        "schema_version": 1,
        "sample_type": "independent_factor_signal_sample",
        "factor_id": str(fid),
        "research_epoch": config.RESEARCH_EPOCH,
        "recipe_sha256": row["sealed_recipe_sha256"],
        "data_key": verified["test"]["data_key"],
        "producer": {"type": "independent_verifier", **verifier_binding},
        "dates": verified["payload"]["window"]["dates"][
            :int(config.FORWARD_SHADOW_MIN_DAYS)
        ],
        "securities": primary_securities,
        "values": independent_matrix,
    }
    independent_path, independent_sha256 = _write_content_addressed_payload(
        "reports/factor_signal_samples/independent", independent_sample,
    )
    rank_correlation, max_abs_difference = _signal_sample_comparison(
        primary_matrix, independent_matrix,
    )
    test = verified["test"]
    window = verified["payload"]["window"]
    forward_binding = {
        "test_epoch": str(test["epoch"]),
        "path": str(test["evidence_path"]),
        "sha256": str(test["evidence_sha256"]),
        "data_key": str(test["data_key"]),
        "evaluator_fingerprint": str(test["evaluator_fingerprint"]),
    }
    reproduction = {
        "schema_version": 1,
        "artifact_type": "independent_factor_reproduction",
        "factor_id": str(fid),
        "research_epoch": config.RESEARCH_EPOCH,
        "research_record_sha256": row["research_record_sha256"],
        "recipe_sha256": row["sealed_recipe_sha256"],
        "execution_spec": {"hold_days": int(verified["hold_days"])},
        "forward_evidence": forward_binding,
        "window": {
            "start": window["start"], "end": window["end"],
            "n_dates": int(window["n_dates"]),
        },
        "verifier_source": verifier_binding,
        "verifier_commitment": {
            "path": commitment["path"], "sha256": commitment["sha256"],
        },
        "independent_input": input_binding,
        "runner_attestation": attestation,
        "signal_samples": {
            "primary": primary_binding,
            "independent": {
                "path": independent_path, "sha256": independent_sha256,
            },
        },
        "comparison": {
            "n_dates": int(config.FORWARD_SHADOW_MIN_DAYS),
            "n_securities": len(primary_securities),
            "rank_correlation": rank_correlation,
            "max_abs_difference": max_abs_difference,
        },
    }
    reproduction_path, reproduction_sha256 = _write_content_addressed_payload(
        "reports/factor_independent_reproduction", reproduction,
    )
    hold_days = int(verified["hold_days"])
    certificate = {
        "schema_version": 1,
        "certificate_type": "independent_production_admission",
        "factor_id": str(fid),
        "research_epoch": config.RESEARCH_EPOCH,
        "bindings": {
            "research_record_sha256": row["research_record_sha256"],
            "recipe_sha256": row["sealed_recipe_sha256"],
            "forward_evidence": forward_binding,
            "independent_reproduction": {
                "path": reproduction_path, "sha256": reproduction_sha256,
            },
            "verifier_source": verifier_binding,
            "verifier_commitment": {
                "path": commitment["path"], "sha256": commitment["sha256"],
            },
        },
        "policy": {
            "required_forward_dates": (
                int(config.FORWARD_SHADOW_MIN_DAYS) + hold_days + 1
            ),
            "completed_signal_dates": int(config.FORWARD_SHADOW_MIN_DAYS),
            "hold_days": hold_days,
            "minimum_securities": _ADMISSION_MIN_SECURITIES,
            "minimum_rank_correlation": _ADMISSION_MIN_RANK_CORRELATION,
            "maximum_abs_difference": _ADMISSION_MAX_ABS_DIFFERENCE,
        },
    }
    certificate_path, certificate_sha256 = _write_content_addressed_payload(
        "factor_system/registry/production_admissions", certificate,
    )
    attach_production_admission_certificate(
        c, str(fid), path=certificate_path, sha256=certificate_sha256,
    )
    return {
        "certificate_path": certificate_path,
        "certificate_sha256": certificate_sha256,
        "reproduction_path": reproduction_path,
        "reproduction_sha256": reproduction_sha256,
        "independent_sample_path": independent_path,
        "independent_sample_sha256": independent_sha256,
        "runner_attestation": attestation,
    }


def attach_production_admission_certificate(
    c: sqlite3.Connection, fid: str, *, path: str, sha256: str,
) -> dict:
    """Attach one immutable externally-produced certificate after full verification."""
    raw = c.execute("SELECT * FROM factors WHERE factor_id=?", (fid,)).fetchone()
    if raw is None:
        raise ValueError(f"factor 不存在: {fid}")
    row = dict(raw)
    if row.get("status") not in {"historical_pass", "backtesting"}:
        raise ValueError("production admission 只能附着到已完成前向检验的候选")
    recipe = _load_frozen_recipe(row)
    verify_factor_source_for_pipeline(c, row, recipe)
    verified = verify_forward_production_semantics(
        c, row, test_epoch=row.get("backtest_epoch"),
        evidence_path=row.get("evidence_path"),
        evidence_sha256=row.get("evidence_sha256"), recipe=recipe,
    )
    verify_production_admission_certificate(
        c, row, recipe, verified, certificate_path=path,
        certificate_sha256=sha256,
    )
    existing = (
        row.get("production_admission_path"),
        row.get("production_admission_sha256"),
    )
    if any(existing) and existing != (path, sha256):
        raise ValueError("production admission certificate 已冻结，不得替换")
    c.execute(
        "UPDATE factors SET production_admission_path=?,"
        "production_admission_sha256=?,updated_at=? WHERE factor_id=?",
        (path, sha256, _now(), fid),
    )
    return dict(c.execute(
        "SELECT * FROM factors WHERE factor_id=?", (fid,),
    ).fetchone())


def _verify_production_commit(c: sqlite3.Connection, fid: str, *,
                              backtest_epoch: str | None,
                              evidence_path: str | None,
                              evidence_sha256: str | None,
                              artifact: dict) -> None:
    """registry 最后一层 fail-closed：任何调用者都不能只凭四个字符串投产。"""
    if not backtest_epoch or not evidence_path or not evidence_sha256:
        raise ValueError("production 缺少冻结前向检验定位")
    row = c.execute("SELECT * FROM factors WHERE factor_id=?", (fid,)).fetchone()
    if row is None or row["research_epoch"] != config.RESEARCH_EPOCH:
        raise ValueError("production 因子不属于当前 research epoch")
    required_seal = (
        "sealed_at", "sealed_data_end", "sealed_recipe_sha256",
        "research_record_json", "research_record_sha256",
    )
    if any(not row[key] for key in required_seal):
        raise ValueError("production 因子缺少不可变 recipe seal")
    recipe = _load_frozen_recipe(dict(row))
    verify_factor_source_for_pipeline(c, dict(row), recipe)

    verified = verify_forward_production_semantics(
        c, dict(row), test_epoch=backtest_epoch,
        evidence_path=evidence_path, evidence_sha256=evidence_sha256,
        recipe=recipe,
    )
    test = verified["test"]
    admission_path = row["production_admission_path"]
    admission_sha256 = row["production_admission_sha256"]
    if not admission_path or not admission_sha256:
        raise ValueError("production 缺少独立 admission certificate")
    verify_production_admission_certificate(
        c, dict(row), recipe, verified, certificate_path=admission_path,
        certificate_sha256=admission_sha256,
    )

    artifact_path, score = _verified_project_file(
        artifact["path"], artifact["sha256"], label="production 分数制品",
    )
    from .data import PANEL_UNIVERSE_SCOPE
    from .production import evaluator_fingerprint, process_evaluator_fingerprint
    current_evaluator = evaluator_fingerprint()
    if current_evaluator != process_evaluator_fingerprint():
        raise ValueError("production evaluator 源码已热变更，必须重启后再提交")
    if (artifact_path.name != f"{artifact['sha256']}.json"
            or test["data_key"] != artifact["data_key"]
            or score.get("schema_version") != 4
            or score.get("factor_id") != fid
            or score.get("research_epoch") != config.RESEARCH_EPOCH
            or score.get("recipe_sha256") != row["sealed_recipe_sha256"]
            or score.get("research_record_sha256")
            != row["research_record_sha256"]
            or score.get("forward_evidence_path") != evidence_path
            or score.get("forward_evidence_sha256") != evidence_sha256
            or score.get("production_admission_path") != admission_path
            or score.get("production_admission_sha256") != admission_sha256
            or score.get("data_key") != artifact["data_key"]
            or score.get("as_of") != artifact["as_of"]
            or score.get("evaluator_fingerprint") != current_evaluator
            or artifact.get("evaluator_fingerprint") != current_evaluator
            or score.get("normalization") != "sh_sz_main_chinext_zscore_ddof0"
            or score.get("universe_scope") != PANEL_UNIVERSE_SCOPE
            or not isinstance(score.get("scores"), dict)
            or int(score.get("n_scores") or 0) != len(score["scores"])
            or not score["scores"]):
        raise ValueError("production 分数制品身份/输入/hash 复核失败")


def reset_evaluator_seal(c: sqlite3.Connection, fid: str, *,
                         sealed_data_end: str, reason: str) -> dict:
    """Start a fresh forward clock without rewriting historical test evidence."""
    try:
        sealed_end = pd.Timestamp(sealed_data_end)
        if pd.isna(sealed_end):
            raise ValueError
    except (TypeError, ValueError) as exc:
        raise ValueError("evaluator 数据截止日无效") from exc
    from .production import process_evaluator_fingerprint
    now = _now()
    changed = c.execute(
        """UPDATE factors SET status='historical_pass',status_reason=?,
                 evaluator_sealed_at=?,evaluator_sealed_data_end=?,
                 evaluator_fingerprint=?,updated_at=?
             WHERE factor_id=? AND research_epoch=?
               AND status IN ('backtesting','historical_pass')""",
        (
            reason, now, sealed_end.date().isoformat(),
            process_evaluator_fingerprint(), now, fid, config.RESEARCH_EPOCH,
        ),
    ).rowcount
    if changed != 1:
        raise RuntimeError(f"evaluator 观察起点无法冻结: {fid}")
    return dict(c.execute(
        "SELECT * FROM factors WHERE factor_id=?", (fid,),
    ).fetchone())


def set_backtest(c: sqlite3.Connection, fid: str, status: str, reason: str, metrics: dict,
                 evidence: dict | None = None, *, backtest_epoch: str | None = None,
                 gate_passed: bool | None = None, evidence_path: str | None = None,
                 evidence_sha256: str | None = None,
                 evaluator_sealed_data_end: str | None = None,
                 production_artifact: dict | None = None):
    """记录回测结论。evidence（P2-4 科学闸门）存 JSON：折次/通过率/t统计/各闸门判定。"""
    if evaluator_sealed_data_end is not None and status != "historical_pass":
        raise ValueError("只有 historical_pass 可以冻结 evaluator 观察起点")
    if status == "historical_pass" and evaluator_sealed_data_end is None:
        sealed = c.execute(
            """SELECT evaluator_sealed_at,evaluator_sealed_data_end,
                      evaluator_fingerprint
                 FROM factors WHERE factor_id=?""",
            (fid,),
        ).fetchone()
        if sealed is None or any(not sealed[key] for key in (
            "evaluator_sealed_at", "evaluator_sealed_data_end",
            "evaluator_fingerprint",
        )):
            raise ValueError(
                "historical_pass 必须在实际 Panel 最新日冻结 evaluator 观察起点"
            )
    if status == "production":
        if not isinstance(production_artifact, dict) or any(
            not production_artifact.get(key) for key in _PRODUCTION_ARTIFACT_KEYS
        ):
            raise ValueError("production 必须绑定完整的内容寻址分数制品")
        _verify_production_commit(
            c, fid, backtest_epoch=backtest_epoch,
            evidence_path=evidence_path, evidence_sha256=evidence_sha256,
            artifact=production_artifact,
        )
    elif production_artifact is not None:
        raise ValueError("非 production 状态不得绑定生产分数制品")
    now = _now()
    fold_rate = None
    t_stat = None
    if evidence:
        fold_rate = evidence.get("fold_pass", {}).get("pass_rate") if isinstance(evidence.get("fold_pass"), dict) else None
        t_stat = evidence.get("full", {}).get("t_stat") if isinstance(evidence.get("full"), dict) else None
    oos_p = ((evidence.get("oos_ic") or {}).get("p_value")
             if isinstance(evidence, dict) else None)
    import json as _json
    ev_json = _json.dumps(evidence, ensure_ascii=False, default=str) if evidence else None
    c.execute(
        """UPDATE factors SET
             status=?, status_reason=?,
             backtest_annual_return=?, backtest_sharpe=?, backtest_max_drawdown=?,
             backtest_win_rate=?, backtest_excess_annual=?, backtest_benchmark_annual=?,
             backtest_fold_pass_rate=?, backtest_t_stat=?, backtest_evidence=?,
             backtest_epoch=?, oos_test_p_value=?, backtest_gate_passed=?,
             evidence_path=?, evidence_sha256=?,
             updated_at=?
           WHERE factor_id=?""",
        (
            status, reason,
            metrics.get("annual_return"), metrics.get("sharpe"),
            metrics.get("max_drawdown"), metrics.get("win_rate"),
            metrics.get("excess_annual"), metrics.get("benchmark_annual"),
            fold_rate, t_stat, ev_json,
            backtest_epoch, oos_p,
            (None if gate_passed is None else int(bool(gate_passed))),
            evidence_path, evidence_sha256,
            now, fid,
        ),
    )
    if status == "historical_pass" and evaluator_sealed_data_end is not None:
        reset_evaluator_seal(
            c, fid, sealed_data_end=evaluator_sealed_data_end,
            reason=reason,
        )
    if status == "production":
        c.execute(
            """UPDATE factors SET
                 production_score_path=?,production_score_sha256=?,
                 production_data_key=?,production_as_of=?,
                 production_evaluator_sha256=?
               WHERE factor_id=?""",
            (
                production_artifact["path"], production_artifact["sha256"],
                production_artifact["data_key"], production_artifact["as_of"],
                production_artifact["evaluator_fingerprint"],
                fid,
            ),
        )
        row = c.execute(
            "SELECT research_epoch FROM factors WHERE factor_id=?", (fid,),
        ).fetchone()
        if row is None or not row["research_epoch"]:
            raise RuntimeError(f"production 因子缺少 research epoch: {fid}")
        _refresh_production_catalog(c, row["research_epoch"])
    if status in _TERMINAL_FACTOR_STATUSES:
        _append_factor_trial_snapshot(
            c, fid, event=f"terminal_backtest:{status}",
        )


def _ensure_test_epoch(c: sqlite3.Connection, *, epoch: str, q: float,
                       dependent: bool, method_version: str,
                       p_value_method: str, data_key: str) -> None:
    meta = {
        "q": float(q), "dependent": int(bool(dependent)), "method": "LOND",
        "method_version": str(method_version),
        "p_value_method": str(p_value_method),
        "initial_data_key": str(data_key),
    }
    frozen = c.execute("SELECT * FROM test_epochs WHERE epoch=?", (epoch,)).fetchone()
    if frozen is None:
        c.execute(
            """INSERT INTO test_epochs
                 (epoch,q,dependent,method,method_version,p_value_method,
                  initial_data_key,created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (epoch, meta["q"], meta["dependent"], meta["method"],
             meta["method_version"], meta["p_value_method"],
             meta["initial_data_key"], _now()),
        )
        return
    for key in (
        "q", "dependent", "method", "method_version", "p_value_method",
    ):
        if frozen[key] != meta[key]:
            raise ValueError(f"test epoch 统计口径已冻结但输入变化: {epoch}")


def begin_factor_observation(c: sqlite3.Connection, fid: str, *, epoch: str,
                             q: float = config.FDR_Q,
                             dependent: bool = True,
                             method_version: str = "lond-dependent-v1",
                             p_value_method: str = "non_overlapping_phase_max_hac",
                             data_key: str = "unspecified",
                             producer_type: str = "unspecified",
                             context: dict | None = None) -> dict:
    """在评估前冻结到达顺序；pending 假设会阻止后续假设越过。"""
    context_json = json.dumps(
        context or {}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )
    context_sha256 = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    from .production import forward_test_epoch, process_evaluator_fingerprint
    observation_evaluator = ""
    if producer_type == "factor_backtest":
        observation_evaluator = process_evaluator_fingerprint()
        if (not isinstance(context, dict)
                or context.get("evaluator_fingerprint") != observation_evaluator):
            raise RuntimeError(
                f"factor observation evaluator 上下文未冻结: {epoch}/{fid}"
            )
    if epoch == forward_test_epoch():
        factor = c.execute(
            """SELECT research_epoch,evaluator_sealed_at,
                      evaluator_sealed_data_end,evaluator_fingerprint
                 FROM factors WHERE factor_id=?""",
            (fid,),
        ).fetchone()
        if (factor is None or factor["research_epoch"] != config.RESEARCH_EPOCH
                or not factor["evaluator_sealed_at"]
                or not factor["evaluator_sealed_data_end"]
                or factor["evaluator_fingerprint"] != observation_evaluator):
            raise RuntimeError(
                f"forward observation evaluator seal 未授权，禁止消费 LOND: {fid}"
            )
    _ensure_test_epoch(
        c, epoch=epoch, q=q, dependent=dependent,
        method_version=method_version, p_value_method=p_value_method,
        data_key=data_key,
    )
    pending = c.execute(
        "SELECT * FROM test_observation_latches WHERE epoch=?", (epoch,),
    ).fetchone()
    if pending is not None:
        if (pending["factor_id"] != fid
                or pending["data_key"] != str(data_key)
                or pending["p_value_method"] != str(p_value_method)
                or pending["producer_type"] != str(producer_type)
                or pending["context_sha256"] != context_sha256
                or pending["evaluator_fingerprint"] != observation_evaluator):
            raise RuntimeError(
                "存在未完成的更早假设，拒绝后续检验越过: "
                f"{epoch}/{pending['factor_id']}"
            )
        return {"status": "pending", **dict(pending)}
    final = c.execute(
        "SELECT * FROM factor_tests WHERE epoch=? AND factor_id=?", (epoch, fid),
    ).fetchone()
    if final is not None:
        if (final["p_value_method"] != str(p_value_method)
                or final["data_key"] != str(data_key)
                or final["evaluator_fingerprint"] != observation_evaluator):
            raise ValueError(f"factor test 已冻结但输入变化: {epoch}/{fid}")
        return {"status": "final", "test": dict(final)}
    reserved_at = _now()
    c.execute(
        """INSERT INTO test_observation_latches
             (epoch,factor_id,data_key,p_value_method,producer_type,
              context_json,context_sha256,evaluator_fingerprint,reserved_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (epoch, fid, str(data_key), str(p_value_method), str(producer_type),
         context_json, context_sha256, observation_evaluator, reserved_at),
    )
    return {
        "status": "pending", "epoch": epoch, "factor_id": fid,
        "data_key": str(data_key), "p_value_method": str(p_value_method),
        "producer_type": str(producer_type), "context_json": context_json,
        "context_sha256": context_sha256,
        "evaluator_fingerprint": observation_evaluator,
        "reserved_at": reserved_at,
    }


def pending_factor_observation(c: sqlite3.Connection, epoch: str) -> dict | None:
    """读取某统计 epoch 唯一未完成观察，供生产者精确恢复。"""
    row = c.execute(
        "SELECT * FROM test_observation_latches WHERE epoch=?", (epoch,),
    ).fetchone()
    return dict(row) if row is not None else None


def frozen_test_epoch(c: sqlite3.Connection, epoch: str) -> dict:
    """返回预约时已冻结的 LOND 口径，供崩溃恢复原样终结。"""
    row = c.execute("SELECT * FROM test_epochs WHERE epoch=?", (epoch,)).fetchone()
    if row is None:
        raise RuntimeError(f"pending 观察缺少冻结 test epoch: {epoch}")
    if row["method"] != "LOND":
        raise RuntimeError(f"pending 观察统计方法损坏: {epoch}")
    return dict(row)


def record_factor_test(c: sqlite3.Connection, fid: str, *, epoch: str,
                       p_value: float | None, gate_passed: bool,
                       evidence_path: str | None = None,
                       evidence_sha256: str | None = None,
                       q: float = config.FDR_Q,
                       dependent: bool = True,
                       method_version: str = "lond-dependent-v1",
                       p_value_method: str = "non_overlapping_phase_max_hac",
                       data_key: str = "unspecified",
                       require_observation: bool = False,
                       producer_type: str | None = None,
                       context: dict | None = None) -> dict:
    """按到达顺序为 24h 无限假设流记一次不可变的 LOND 判定。"""
    try:
        p = float(p_value)
        if not 0.0 <= p <= 1.0:
            p = 1.0
    except (TypeError, ValueError):
        p = 1.0
    if not c.in_transaction:
        c.execute("BEGIN IMMEDIATE")
    _ensure_test_epoch(
        c, epoch=epoch, q=q, dependent=dependent,
        method_version=method_version, p_value_method=p_value_method,
        data_key=data_key,
    )
    pending = c.execute(
        "SELECT * FROM test_observation_latches WHERE epoch=?", (epoch,),
    ).fetchone()
    if pending is not None and pending["factor_id"] != fid:
        raise RuntimeError(
            "存在未完成的更早假设，拒绝后续检验越过: "
            f"{epoch}/{pending['factor_id']}"
        )
    if pending is not None and (
        pending["data_key"] != str(data_key)
        or pending["p_value_method"] != str(p_value_method)
    ):
        raise ValueError(f"factor test 观察上下文已冻结但输入变化: {epoch}/{fid}")
    test_evaluator = pending["evaluator_fingerprint"] if pending is not None else ""
    if pending is not None and producer_type is not None:
        context_json = json.dumps(
            context or {}, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), default=str,
        )
        context_sha256 = hashlib.sha256(context_json.encode("utf-8")).hexdigest()
        if (pending["producer_type"] != str(producer_type)
                or pending["context_sha256"] != context_sha256):
            raise ValueError(
                f"factor test 观察生产者/恢复上下文已冻结但输入变化: {epoch}/{fid}"
            )
        if producer_type == "factor_backtest":
            from .production import process_evaluator_fingerprint
            active_evaluator = process_evaluator_fingerprint()
            if (context or {}).get("evaluator_fingerprint") != test_evaluator:
                raise ValueError(
                    f"factor test evaluator 上下文已冻结但输入变化: {epoch}/{fid}"
                )
            if active_evaluator != test_evaluator and (gate_passed or p != 1.0):
                raise RuntimeError(
                    f"旧 evaluator pending 只能保守终结: {epoch}/{fid}"
                )
    existing = c.execute(
        "SELECT * FROM factor_tests WHERE epoch=? AND factor_id=?", (epoch, fid),
    ).fetchone()
    if require_observation and pending is None and existing is None:
        raise RuntimeError(f"factor test 未预先冻结观察顺序: {epoch}/{fid}")
    if existing is not None:
        if (abs(float(existing["p_value"]) - p) > 1e-12
                or bool(existing["gate_passed"]) != bool(gate_passed)
                or existing["p_value_method"] != str(p_value_method)
                or existing["data_key"] != str(data_key)
                or existing["evaluator_fingerprint"] != test_evaluator):
            raise ValueError(f"factor test 已冻结但输入变化: {epoch}/{fid}")
        c.execute(
            "DELETE FROM test_observation_latches WHERE epoch=? AND factor_id=?",
            (epoch, fid),
        )
        return dict(existing)

    from quantqc.stats import online_lond_alpha
    row = c.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(online_reject),0) AS d "
        "FROM factor_tests WHERE epoch=?", (epoch,),
    ).fetchone()
    order = int(row["n"]) + 1
    alpha_i = online_lond_alpha(
        order, int(row["d"]), q=q, dependent=dependent,
    )
    reject = bool(gate_passed and p <= alpha_i)
    c.execute(
        """INSERT INTO factor_tests
             (epoch,factor_id,hypothesis_order,p_value,alpha_i,gate_passed,
              online_reject,tested_at,data_key,p_value_method,
              evaluator_fingerprint,evidence_path,evidence_sha256)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (epoch, fid, order, p, alpha_i, int(bool(gate_passed)), int(reject),
         _now(), str(data_key), str(p_value_method), test_evaluator,
         evidence_path, evidence_sha256),
    )
    c.execute(
        "DELETE FROM test_observation_latches WHERE epoch=? AND factor_id=?",
        (epoch, fid),
    )
    return dict(c.execute(
        "SELECT * FROM factor_tests WHERE epoch=? AND factor_id=?", (epoch, fid),
    ).fetchone())


def attach_factor_test_evidence(c: sqlite3.Connection, test_id: int, *,
                                evidence_path: str, evidence_sha256: str) -> None:
    """只给已冻结的在线检验补充不可变证据定位；不改 p/alpha/判定。"""
    row = c.execute(
        "SELECT evidence_path,evidence_sha256 FROM factor_tests WHERE test_id=?",
        (test_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"factor test 不存在: {test_id}")
    old_path, old_sha = row["evidence_path"], row["evidence_sha256"]
    if old_path is not None or old_sha is not None:
        if old_path != evidence_path or old_sha != evidence_sha256:
            raise ValueError(f"factor test 证据已冻结: {test_id}")
        return
    c.execute(
        "UPDATE factor_tests SET evidence_path=?,evidence_sha256=? WHERE test_id=?",
        (evidence_path, evidence_sha256, test_id),
    )


def freeze(c: sqlite3.Connection, fid: str, reason: str = "连续N日IC转负"):
    set_status(c, fid, "frozen", reason)


def _factor_dict(row: sqlite3.Row) -> dict:
    out = dict(row)
    try:
        failure = json.loads(out.get("failure_json") or "null")
    except (TypeError, json.JSONDecodeError):
        failure = None
    out["failure_diagnostic"] = failure if isinstance(failure, dict) else None
    return out


def all_factors(c: sqlite3.Connection) -> list[dict]:
    rows = c.execute("SELECT * FROM factors ORDER BY created_at DESC").fetchall()
    return [_factor_dict(r) for r in rows]


_PUBLIC_ACTIVE_STATUSES = frozenset({
    "candidate", "validated", "registered", "backtesting",
    "historical_pass", "production",
})
_PUBLIC_STRATEGY_STATUSES = frozenset({"historical_pass", "production"})


def _factor_columns(c: sqlite3.Connection) -> set[str]:
    return {str(row[1]) for row in c.execute("PRAGMA table_info(factors)")}


def _public_factor_where(*, columns: set[str], research_epoch: str, scope: str,
                         statuses: tuple[str, ...], query: str
                         ) -> tuple[str, list[object]]:
    """Build the public registry predicate without materialising the table."""
    clauses: list[str] = []
    params: list[object] = []
    if scope == "current":
        clauses.append("research_epoch IS ?")
        params.append(research_epoch)
    elif scope == "archive":
        clauses.append("research_epoch IS NOT ?")
        params.append(research_epoch)
    elif scope != "all":
        raise ValueError("scope must be all, current, or archive")

    if statuses:
        status_terms: list[str] = []
        ordinary = tuple(value for value in statuses if value != "invalidated")
        if "invalidated" in statuses:
            status_terms.append("(research_epoch IS NOT ? OR status='invalidated')")
            params.append(research_epoch)
        if ordinary:
            placeholders = ",".join("?" for _ in ordinary)
            status_terms.append(
                f"(research_epoch IS ? AND status IN ({placeholders}))"
            )
            params.extend((research_epoch, *ordinary))
        clauses.append("(" + " OR ".join(status_terms) + ")")

    if query:
        escaped = (
            query.casefold().replace("\\", "\\\\")
            .replace("%", "\\%").replace("_", "\\_")
        )
        fields = tuple(field for field in (
            "factor_id", "name", "family", "formula", "description",
            "status", "status_reason",
        ) if field in columns)
        if fields:
            clauses.append("(" + " OR ".join(
                f"LOWER(COALESCE({field},'')) LIKE ? ESCAPE '\\'"
                for field in fields
            ) + ")")
            params.extend([f"%{escaped}%"] * len(fields))
        else:
            clauses.append("0=1")
    return (" AND ".join(clauses) if clauses else "1=1"), params


def public_factor_page(c: sqlite3.Connection, *, research_epoch: str,
                       scope: str = "all", statuses: tuple[str, ...] = (),
                       query: str = "", limit: int = 50, offset: int = 0,
                       sort: str = "created_at") -> tuple[list[dict], int]:
    """Return one public page and its filtered count using bounded SQL work."""
    if int(limit) < 1 or int(offset) < 0:
        raise ValueError("invalid page boundary")
    columns = _factor_columns(c)
    where, params = _public_factor_where(
        columns=columns, research_epoch=research_epoch, scope=scope,
        statuses=tuple(statuses), query=query,
    )
    total = int(c.execute(
        f"SELECT COUNT(*) FROM factors WHERE {where}", params,
    ).fetchone()[0])
    if sort == "created_at":
        order = "created_at DESC, factor_id ASC"
    else:
        evidence = "backtest_evidence" if "backtest_evidence" in columns else "NULL"
        historical_ic = (
            "CASE WHEN json_valid(backtest_evidence) "
            "THEN json_extract(backtest_evidence,'$.oos_ic.ic_mean') END"
        ).replace("backtest_evidence", evidence)
        historical_ir = (
            "CASE WHEN json_valid(backtest_evidence) "
            "THEN json_extract(backtest_evidence,'$.oos_ic.ic_ir') END"
        ).replace("backtest_evidence", evidence)
        raw_ic = "oos_ic_mean" if "oos_ic_mean" in columns else "NULL"
        raw_ir = "oos_ic_ir" if "oos_ic_ir" in columns else "NULL"
        eligible = "research_epoch IS ? AND status!='invalidated'"
        if sort == "ic":
            metric = f"CASE WHEN {eligible} THEN COALESCE({historical_ic},{raw_ic}) END"
        elif sort == "icir":
            metric = f"CASE WHEN {eligible} THEN ABS(COALESCE({historical_ir},{raw_ir})) END"
        elif sort == "abs_ic":
            metric = f"CASE WHEN {eligible} THEN ABS(COALESCE({historical_ic},{raw_ic})) END"
        else:
            raise ValueError("sort must be created_at, abs_ic, ic, or icir")
        order = f"({metric}) IS NULL ASC, ({metric}) DESC, created_at DESC, factor_id ASC"
        params = [*params, research_epoch, research_epoch]
    rows = c.execute(
        f"SELECT * FROM factors WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, int(limit), int(offset)),
    ).fetchall()
    return [_factor_dict(row) for row in rows], total


def public_factor_count(c: sqlite3.Connection, *, research_epoch: str,
                        scope: str = "all", statuses: tuple[str, ...] = (),
                        query: str = "") -> int:
    where, params = _public_factor_where(
        columns=_factor_columns(c), research_epoch=research_epoch, scope=scope,
        statuses=tuple(statuses), query=query,
    )
    return int(c.execute(
        f"SELECT COUNT(*) FROM factors WHERE {where}", params,
    ).fetchone()[0])


def public_registry_summary(c: sqlite3.Connection, *, research_epoch: str) -> dict:
    """Aggregate public lifecycle and audit counts in SQLite."""
    columns = _factor_columns(c)
    rows = c.execute(
        """SELECT
             CASE WHEN research_epoch IS ? THEN 'current' ELSE 'archive' END AS scope,
             CASE WHEN research_epoch IS ? THEN COALESCE(status,'candidate')
                  ELSE 'invalidated' END AS public_status,
             COUNT(*) AS n
           FROM factors
          GROUP BY scope, public_status""",
        (research_epoch, research_epoch),
    ).fetchall()
    summary = {
        scope: {"total": 0, "runnable_total": 0,
                "terminal_total": 0, "by_status": {}}
        for scope in ("current", "archive")
    }
    for row in rows:
        scope = str(row["scope"])
        status = str(row["public_status"])
        count = int(row["n"])
        summary[scope]["by_status"][status] = count
        summary[scope]["total"] += count
        if status in _PUBLIC_ACTIVE_STATUSES:
            summary[scope]["runnable_total"] += count
        else:
            summary[scope]["terminal_total"] += count
    eligibility = {
        name: {
            "runnable_scope": "research_pipeline_only",
            "research_pipeline_active_total": row["runnable_total"],
            "research_strategy_eligible_total": sum(
                row["by_status"].get(status, 0)
                for status in _PUBLIC_STRATEGY_STATUSES
            ),
            "production_consumer_ready_total": int(
                row["by_status"].get("production", 0)
            ),
        }
        for name, row in summary.items()
    }
    updated = "updated_at" if "updated_at" in columns else "NULL"
    research_record = (
        "research_record_json" if "research_record_json" in columns else "NULL"
    )
    evidence = "backtest_evidence" if "backtest_evidence" in columns else "NULL"
    status_reason = "status_reason" if "status_reason" in columns else "NULL"
    audit = c.execute(
        f"""SELECT
             MAX({updated}) AS as_of,
             SUM(CASE WHEN {research_record} IS NULL OR TRIM({research_record})=''
                      THEN 1 ELSE 0 END) AS legacy_unsourced,
             SUM(CASE WHEN research_epoch IS NOT ? OR status='invalidated'
                           OR {evidence} IS NULL
                           OR NOT json_valid({evidence})
                      THEN 1 ELSE 0 END) AS no_evidence,
             SUM(CASE WHEN research_epoch IS ? AND status!='invalidated'
                           AND json_valid({evidence})
                           AND json_extract({evidence},'$.verdict')='NO-GO'
                      THEN 1 ELSE 0 END) AS nogo,
             SUM(CASE WHEN research_epoch IS ? AND status!='invalidated'
                           AND json_valid({evidence})
                           AND COALESCE(json_extract({evidence},'$.verdict'),'')!='NO-GO'
                           AND COALESCE({status_reason},'') LIKE '%FDR%'
                      THEN 1 ELSE 0 END) AS fdr
           FROM factors""",
        (research_epoch, research_epoch, research_epoch),
    ).fetchone()
    as_of = audit["as_of"]
    version = hashlib.sha256(json.dumps({
        "research_epoch": research_epoch,
        "as_of": as_of,
        "counts": summary, "eligibility": eligibility,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )).hexdigest()[:16]
    return {
        **summary,
        "eligibility": eligibility,
        "version": version,
        "as_of": as_of,
        "total": summary["current"]["total"] + summary["archive"]["total"],
        "legacy_unsourced": int(audit["legacy_unsourced"] or 0),
        "no_evidence": int(audit["no_evidence"] or 0),
        "nogo": int(audit["nogo"] or 0),
        "fdr": int(audit["fdr"] or 0),
    }


def factors_by_status(c: sqlite3.Connection, *statuses: str) -> list[dict]:
    if not statuses:
        return all_factors(c)
    ph = ",".join("?" for _ in statuses)
    rows = c.execute(f"SELECT * FROM factors WHERE status IN ({ph}) ORDER BY created_at DESC", statuses).fetchall()
    return [_factor_dict(r) for r in rows]


def registered_awaiting_backtest(c: sqlite3.Connection,
                                 epoch: str | None = None) -> list[dict]:
    """待历史确认队列；严格 epoch 调用不会误消费旧研究记录。"""
    sql = (
        "SELECT factors.* FROM factors WHERE status='registered' "
        "AND (NOT EXISTS (SELECT 1 FROM candidate_blind_runs "
        "WHERE candidate_id=factors.factor_id) "
        "OR EXISTS (SELECT 1 FROM blind_post_admissions "
        "WHERE candidate_id=factors.factor_id)) "
    )
    params: tuple = ()
    if epoch is not None:
        sql += "AND research_epoch=? "
        params = (epoch,)
    rows = c.execute(sql + "ORDER BY created_at DESC", params).fetchall()
    return [dict(r) for r in rows]


def set_production(c: sqlite3.Connection, fid: str, metrics: dict, evidence: dict | None = None,
                   **kwargs):
    """旧兼容入口已禁用；只能由 production.promote_factor 完成原子投产。"""
    raise RuntimeError(
        "set_production 已禁用；必须生成并复核前向证据与内容寻址分数制品"
    )
