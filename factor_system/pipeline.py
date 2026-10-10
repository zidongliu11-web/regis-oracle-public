"""端到端编排（任务书第十四部分·诚实适配）。

两区独立运行：
  mining   ：发现段挖候选 → 独立验证段 L0-L3 → 入库 → 配方存档
  backtest ：消费 registered 因子 → 历史确认 + 在线 FDR → historical_pass/rejected；
             仅 2026+ 前向观察满 120 日后的一次终测可进入 production

全程写 state/mining.json 与 state/backtest.json，供 5200 仪表盘展示。
8GB 单机：串行、逐候选释放内存、单批上限 MAX_CANDIDATES_PER_RUN。
"""

from __future__ import annotations

from copy import deepcopy
import gc
import json
import datetime
import fcntl
import hashlib
from importlib import metadata
import math
import os
from pathlib import Path
import platform

import numpy as np
import pandas as pd

from . import config
from . import production
from . import registry as reg
from . import source_gate
from . import status
from .data import (EXECUTABLE_FORWARD_LABEL, Panel, _panel_cache_path,
                   executable_forward_return, load_panel, load_thin_panel,
                   load_verified_panel_snapshot, mask_signal_eligible,
                   split_research_windows)
from .gate import research_input_key, run_scientific_gate
from .metrics import cross_sectional_ic, ic_stats
from .preprocess import build_exposures, neutralize_mode_label
from .prescreen import build_reference_library, prescreen_ic_correlation, prescreen_incremental
from .search import (Candidate, compute_factor, generate_candidates,
                     generate_source_candidates, rebuild_factor, to_recipe)
from .validate import l3_fdr, run_validation


_MINING_EVALUATOR_SOURCE_FILES = (
    "pipeline.py", "search.py", "validate.py", "data.py", "../quantqc/ashare.py",
)
_BLIND_RUN_TERMINAL_STATES = frozenset({"complete", "consumed_unknown"})
_BLIND_EVALUATOR_SOURCE_PATHS = (
    "factor_system/pipeline.py",
    "factor_system/search.py",
    "factor_system/validate.py",
    "factor_system/data.py",
    "factor_system/config.py",
    "factor_system/metrics.py",
    "factor_system/diagnostics.py",
    "factor_system/preprocess.py",
    "factor_system/prescreen.py",
    "factor_system/registry.py",
    "factor_system/source_gate.py",
    "factor_system/operators.py",
    "factor_system/templates.py",
    "factor_system/v4_panel.py",
    "quantqc/stats.py",
    "quantqc/ashare.py",
)
_BLIND_EVALUATOR_CONFIG_KEYS = (
    "RESEARCH_EPOCH",
    "L0_CONTRACT_VERSION",
    "DISCOVERY_START",
    "DISCOVERY_END",
    "VALIDATION_START",
    "VALIDATION_END",
    "MIN_BARS",
    "MIN_IC_DATES",
    "IC_THRESHOLD",
    "IC_IR_THRESHOLD",
    "OOS_MAX_IC_DEGRADE",
    "MIN_QUANTILE_SPREAD",
    "QUANTILE",
    "FDR_Q",
    "COSTS",
    "HOLD_DAYS",
)
_BLIND_EVALUATOR_RUNTIME_PACKAGES = {
    "numpy": "numpy",
    "pandas": "pandas",
    "scipy": "scipy",
    "pyarrow": "pyarrow",
}


def _mining_evaluator_fingerprint() -> str:
    source_dir = config.PROJECT_ROOT / "factor_system"
    manifest = {
        name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest()
        for name in _MINING_EVALUATOR_SOURCE_FILES
    }
    body = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


_PROCESS_MINING_EVALUATOR_FINGERPRINT = _mining_evaluator_fingerprint()

# ════════════════════════════════════════════════════════════════
# 挖掘区
# ════════════════════════════════════════════════════════════════

def _save_recipe(fid: str, cand: Candidate, evidence: dict | None = None, *,
                 sealed_at: str, sealed_data_end: str,
                 blind_authorization_sha256: str | None = None,
                 connection=None) -> dict:
    """原子存档不可变配方，并返回最终采用的 seal。

    同一 epoch 的同一 ``factor_id`` 一旦落盘，只允许完全相同的因子定义重放；
    不再用后续批次的新诊断覆盖它。若上次进程恰好在文件落盘后、数据库提交前
    中断，则复用文件中的 seal 完成幂等恢复。
    """
    if blind_authorization_sha256 is None:
        reg.verify_candidate_source(cand)
    else:
        if connection is None:
            raise ValueError("blind 配方冻结缺少 registry connection")
        reg.verify_blind_candidate_source(
            cand, blind_authorization_sha256, connection,
        )
    rdir = config.REGISTRY_DIR / "recipes"
    rdir.mkdir(parents=True, exist_ok=True)
    path = rdir / f"{fid}.json"
    definition = {
        **to_recipe(cand),
        "research_record": _json_safe(cand.research_record),
        "research_record_sha256": cand.research_record_sha256,
    }
    if not definition["research_record"] or not definition["research_record_sha256"]:
        raise ValueError("[source_record_missing] 因子配方缺少来源冻结记录")
    if path.exists():
        existing = json.loads(path.read_text("utf-8"))
        if existing.get("research_epoch") == config.RESEARCH_EPOCH:
            old_definition = {key: existing.get(key) for key in definition}
            if old_definition != definition:
                raise ValueError(f"当前 epoch 的因子配方已冻结但定义变化: {fid}")
            old_at = existing.get("sealed_at")
            old_end = existing.get("sealed_data_end")
            if bool(old_at) != bool(old_end):
                raise ValueError(f"因子配方 seal 不完整: {fid}")
            if old_at and old_end:
                return {
                    "path": path,
                    "sha256": _sha256_file(path),
                    "sealed_at": str(old_at),
                    "sealed_data_end": str(old_end),
                }

    recipe = dict(definition)
    if evidence:
        ev = {}
        for k in ("neutralize_mode", "raw_ic_mean", "raw_ic_ir", "is_ic_mean",
                  "is_ic_ir", "oos_ic_mean", "oos_ic_ir", "ic_retention", "diag"):
            if k in evidence:
                ev[k] = evidence[k]
        recipe["evidence"] = ev
        recipe["research_epoch"] = evidence.get("research_epoch")
        recipe["validation_data_key"] = evidence.get("validation_data_key")
    recipe["recipe_schema_version"] = 3
    recipe["sealed_at"] = sealed_at
    recipe["sealed_data_end"] = sealed_data_end
    body = (json.dumps(_json_safe(recipe), ensure_ascii=False, default=str, sort_keys=True,
                       separators=(",", ":"), allow_nan=False) + "\n")
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as stream:
        stream.write(body)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)
    return {
        "path": path,
        "sha256": _sha256_file(path),
        "sealed_at": sealed_at,
        "sealed_data_end": sealed_data_end,
    }


def _seal_validated_factor(c, panel: Panel, fid: str, cand: Candidate,
                           evidence: dict, *, sealed_at: str | None = None,
                           sealed_data_end: str | None = None,
                           blind_authorization_sha256: str | None = None) -> dict:
    """在入历史确认队列前冻结假设、配方哈希与当时已存在的数据尾。"""
    row = c.execute(
        "SELECT research_epoch,sealed_at,sealed_data_end,sealed_recipe_sha256 "
        "FROM factors WHERE factor_id=?", (fid,),
    ).fetchone()
    if row is None:
        raise ValueError(f"factor 不存在，无法 seal: {fid}")
    if not panel.dates:
        raise ValueError("面板无交易日，无法 seal factor")
    existing = [row[k] for k in (
        "sealed_at", "sealed_data_end", "sealed_recipe_sha256",
    )]
    if any(value is not None for value in existing):
        if not all(value is not None for value in existing):
            raise ValueError(f"factor seal 不完整: {fid}")
        sealed_at = str(row["sealed_at"])
        sealed_data_end = str(row["sealed_data_end"])
    else:
        sealed_at = sealed_at or reg._now()
        sealed_data_end = sealed_data_end or str(pd.Timestamp(max(panel.dates)).date())
    saved = _save_recipe(
        fid, cand, evidence=evidence, sealed_at=sealed_at,
        sealed_data_end=sealed_data_end,
        blind_authorization_sha256=blind_authorization_sha256,
        connection=c,
    )
    reg.seal_factor(
        c, fid, sealed_at=saved["sealed_at"],
        sealed_data_end=saved["sealed_data_end"],
        recipe_sha256=saved["sha256"], research_epoch=config.RESEARCH_EPOCH,
    )
    return saved


def _mining_reject(c, fid: str, cur: str | None, reason: str):
    """挖掘批内拒绝已有因子的统一入口：**绝不降级 production/frozen**。

    2026-08-18 数据完整性修复：此前 L0-L2/M1.4 各拒绝分支无条件 set_status rejected，
    实测旧定时 factor-mining job 重挖模板因子时自比对（IC 相关 1.00）判重复，把已投产的
    F2__n60/F2__n20 从 production 打成 rejected——生产状态被挖掘批抹掉。存废判定归
    生命周期/revalidate 负责，挖掘批只能拒绝「新候选」，对既有 production/frozen 只保留。
    """
    if cur in ("production", "frozen"):
        reg.set_status(c, fid, cur, f"重复挖掘命中，保留{cur}: {reason[:80]}")
    else:
        reg.set_status(c, fid, "rejected", reason)


def _canonical_json_sha256(value: object) -> str:
    body = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _blind_evaluator_identity() -> dict:
    runner_sources = [
        {
            "path": relative,
            "sha256": _sha256_file(config.PROJECT_ROOT / relative),
        }
        for relative in _BLIND_EVALUATOR_SOURCE_PATHS
    ]
    config_values = {
        key: deepcopy(getattr(config, key))
        for key in _BLIND_EVALUATOR_CONFIG_KEYS
    }
    runtime = {
        "python": platform.python_version(),
        **{
            key: metadata.version(distribution)
            for key, distribution in _BLIND_EVALUATOR_RUNTIME_PACKAGES.items()
        },
    }
    frozen = {
        "runner_sources": runner_sources,
        "config_values": config_values,
        "runtime": runtime,
    }
    return {**frozen, "fingerprint": _canonical_json_sha256(frozen)}


def _require_current_blind_evaluator(authorization: dict) -> dict:
    evaluator = authorization.get("evaluator")
    if not isinstance(evaluator, dict):
        raise ValueError("[blind_evaluator_invalid] evaluator 缺失")
    current = _blind_evaluator_identity()
    for field in ("runner_sources", "config_values", "runtime", "fingerprint"):
        if evaluator.get(field) != current[field]:
            raise ValueError(f"[blind_evaluator_drift] {field} 不匹配")
    if evaluator.get("research_epoch") != config.RESEARCH_EPOCH:
        raise ValueError("[blind_evaluator_drift] research_epoch 不匹配")
    return current


def _blind_terminal_summary(row: dict) -> dict:
    state = str(row.get("state") or "")
    return {
        "ok": state == "complete",
        "candidate_id": row.get("candidate_id"),
        "authorization_sha256": row.get("authorization_sha256"),
        "state": state,
        "replayed": True,
        "passed": None,
        "evidence_path": row.get("result_evidence_path"),
        "evidence_sha256": row.get("result_evidence_sha256"),
        "reason": row.get("terminal_reason"),
    }


def _blind_candidate(verification: dict) -> Candidate:
    result = verification.get("candidate_result")
    if not isinstance(result, dict):
        raise ValueError("[blind_authorization_invalid] candidate_result 缺失")
    candidate_id = result.get("candidate_id") or (
        (verification.get("authorization") or {}).get("candidate") or {}
    ).get("id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("[blind_authorization_invalid] candidate_id 缺失")
    matches = [
        cand for cand in generate_source_candidates()
        if cand.factor_id == candidate_id
    ]
    if len(matches) != 1:
        raise RuntimeError("[blind_candidate_identity_invalid] 冻结候选必须唯一")
    record = result.get("research_record")
    digest = result.get("research_record_sha256")
    if not isinstance(record, dict) or not isinstance(digest, str):
        raise ValueError("[blind_authorization_invalid] 来源冻结记录缺失")
    candidate = matches[0]
    candidate.research_record = record
    candidate.research_record_sha256 = digest
    _candidate_validation_policy(candidate)
    return candidate


def _blind_only_source_candidate_ids() -> set[str]:
    """Source candidates with a frozen validation recipe never enter mining."""
    return {
        candidate.factor_id
        for candidate in generate_source_candidates()
        if isinstance(to_recipe(candidate).get("validation_spec"), dict)
    }


def _compute_blind_candidate(panel: Panel, candidate: Candidate) -> pd.DataFrame | None:
    """Compute without the generic search helper's exception swallowing."""
    factor = candidate.compute(panel)
    if factor is None:
        return None
    if not isinstance(factor, pd.DataFrame) or factor.index.empty:
        raise ValueError("[blind_candidate_output_invalid] 因子输出格式无效")
    return mask_signal_eligible(panel, factor)


def _blind_reservation(
    authorization_path: str,
    verification: dict,
    candidate: Candidate,
) -> tuple[dict, dict]:
    authorization = verification["authorization"]
    try:
        chain = authorization["chain"]
        guard_sha256 = chain["blind_evaluation_guard"]["protocol"]["sha256"]
        implementation_files = authorization["implementation"]["files"]
        evaluator = authorization["evaluator"]
        panel_input = authorization["input"]
        authorization_sha256 = verification["authorization_sha256"]
        implementation_audit_sha256 = chain["implementation_audit_sha256"]
    except (KeyError, TypeError) as exc:
        raise ValueError("[blind_authorization_invalid] 授权结构不完整") from exc
    implementation_identity_sha256 = _canonical_json_sha256({
        "files": implementation_files,
    })
    context = {
        "guard_protocol_sha256": guard_sha256,
        "panel_data_key": panel_input["panel_data_key"],
        "stage": authorization["action"],
        "authorization": authorization,
    }
    reservation = {
        "candidate_id": candidate.factor_id,
        "authorization_path": authorization_path,
        "authorization_sha256": authorization_sha256,
        "candidate_research_record_sha256": candidate.research_record_sha256,
        "implementation_identity_sha256": implementation_identity_sha256,
        "implementation_audit_sha256": implementation_audit_sha256,
        "evaluator_fingerprint": evaluator["fingerprint"],
        "panel_path": panel_input["panel_cache_path"],
        "panel_key": panel_input["panel_cache_key"],
        "panel_sha256": panel_input["panel_cache_sha256"],
        "context": context,
    }
    return reservation, context


def _reverify_blind_authorization(
    authorization_path: str,
    authorization_sha256: str,
    frozen: dict,
    *,
    verify_panel: bool,
) -> None:
    current = source_gate.verify_blind_authorization(
        authorization_path, authorization_sha256,
        verify_panel=verify_panel,
    )
    try:
        _require_current_blind_evaluator(frozen["authorization"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("[blind_authorization_drift] evaluator 身份漂移") from exc
    if (
        current.get("valid") is not True
        or current.get("input_verified") is not verify_panel
        or current.get("authorization") != frozen.get("authorization")
        or current.get("candidate_result") != frozen.get("candidate_result")
    ):
        detail = ",".join(current.get("errors") or ["physical_identity_drift"])
        raise RuntimeError(f"[blind_authorization_drift] {detail}")


def _write_blind_run_evidence(
    authorization_sha256: str,
    payload: dict,
) -> tuple[str, str]:
    """Publish one immutable, content-addressed blind-result document."""
    body = (
        json.dumps(
            _json_safe(payload), ensure_ascii=False, sort_keys=True,
            separators=(",", ":"), allow_nan=False,
        ) + "\n"
    ).encode("utf-8")
    evidence_sha256 = hashlib.sha256(body).hexdigest()
    directory = (
        config.REGISTRY_DIR / "evidence" / "blind_runs" / authorization_sha256
    )
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{evidence_sha256}.json"
    if path.exists():
        if not path.is_file() or path.read_bytes() != body:
            raise RuntimeError("blind run 内容寻址证据冲突")
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if not path.is_file() or path.read_bytes() != body:
                    raise RuntimeError("blind run 并发证据冲突")
        finally:
            temporary.unlink(missing_ok=True)
    return str(path.relative_to(config.PROJECT_ROOT)), evidence_sha256


def run_blind_candidate_once(path: str | Path, expected_sha: str) -> dict:
    """Run the one pre-authorized discovery/validation observation.

    This entrypoint never discovers an authorization or a Panel.  Its caller
    must name both by content, and the durable blind-run claim is written
    before the Panel descriptor is opened.
    """
    if not isinstance(path, (str, Path)):
        raise ValueError("blind authorization path 必须是项目内相对路径")
    authorization_path = Path(path).as_posix()
    lock_path = config.STATE_DIR / "blind_candidate_once.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    mutation_lock = lock_path.open("a+", encoding="utf-8")
    try:
        try:
            fcntl.flock(
                mutation_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError as exc:
            raise RuntimeError("另一个一次性盲测调用正在运行") from exc

        reg.migrate_schema()
        connection = reg._conn()
        claim: dict | None = None
        factor_id: str | None = None
        candidate_id: str | None = None
        context: dict | None = None
        owns_active_authorization = False
        try:
            for known_id in _blind_only_source_candidate_ids():
                replay = reg.blind_run_for_candidate(connection, known_id)
                if (
                    replay
                    and replay.get("authorization_path") == authorization_path
                    and replay.get("authorization_sha256") == expected_sha
                    and replay.get("state") in _BLIND_RUN_TERMINAL_STATES
                ):
                    return _blind_terminal_summary(replay)
            verification = source_gate.verify_blind_authorization(
                authorization_path, expected_sha, verify_panel=False,
            )
            authorization_candidate = (
                verification.get("authorization") or {}
            ).get("candidate") or {}
            candidate_id = authorization_candidate.get("id")
            if not isinstance(candidate_id, str) or not candidate_id:
                raise ValueError(
                    "[blind_authorization_invalid] candidate.id 缺失"
                )
            existing = reg.blind_run_for_candidate(
                connection, candidate_id,
            )
            same_authorization = bool(
                existing
                and existing.get("authorization_path") == authorization_path
                and existing.get("authorization_sha256") == expected_sha
            )
            owns_active_authorization = bool(
                same_authorization
                and existing.get("state") in {"reserved", "running"}
            )
            if existing and existing.get("state") in _BLIND_RUN_TERMINAL_STATES:
                if not same_authorization:
                    raise ValueError(
                        "candidate 已永久绑定另一 blind authorization"
                    )
                return _blind_terminal_summary(existing)

            if (
                verification.get("valid") is not True
                or verification.get("input_verified") is not False
            ):
                if existing and same_authorization and existing.get(
                    "state"
                ) in {"reserved", "running"}:
                    reg.consume_blind_run_unknown(
                        connection, expected_sha,
                        context=existing["context"],
                        reason="blind authorization 或物理身份在恢复前漂移",
                    )
                detail = ",".join(
                    verification.get("errors") or ["authorization_invalid"]
                )
                raise ValueError(f"[blind_authorization_invalid] {detail}")

            candidate = _blind_candidate(verification)
            _require_current_blind_evaluator(verification["authorization"])
            reservation, context = _blind_reservation(
                authorization_path, verification, candidate,
            )

            claim = reg.reserve_blind_run(connection, **reservation)
            owns_active_authorization = bool(
                claim.get("authorization_sha256") == expected_sha
                and claim.get("state") in {"reserved", "running"}
            )
            if claim["state"] in _BLIND_RUN_TERMINAL_STATES:
                return _blind_terminal_summary(claim)
            _reverify_blind_authorization(
                authorization_path, expected_sha, verification,
                verify_panel=True,
            )
            claim = reg.mark_blind_run_started(
                connection, expected_sha, context=context,
            )
            if claim["state"] in _BLIND_RUN_TERMINAL_STATES:
                return _blind_terminal_summary(claim)
            if claim["state"] != "running":
                raise RuntimeError("blind run 未进入 running")

            factor_id = reg.register_blind_candidate(
                connection, candidate, expected_sha,
            )
            connection.commit()

            panel_input = verification["authorization"]["input"]
            panel = load_verified_panel_snapshot(
                panel_input["panel_cache_path"],
                panel_input["panel_cache_sha256"],
            )
            loaded_identity = (
                getattr(panel, "_panel_cache_path", None),
                getattr(panel, "_panel_cache_key", None),
                getattr(panel, "_panel_cache_sha256", None),
            )
            authorized_identity = (
                panel_input["panel_cache_path"],
                panel_input["panel_cache_key"],
                panel_input["panel_cache_sha256"],
            )
            if loaded_identity != authorized_identity:
                raise RuntimeError("[blind_panel_identity_drift] Panel 身份不匹配")

            factor = _compute_blind_candidate(panel, candidate)
            policy = _candidate_validation_policy(candidate)
            if factor is None:
                result = {
                    "passed": False,
                    "stage": "compute",
                    "reason": "因子计算失败或全为空",
                    **policy,
                }
            else:
                windows = split_research_windows(panel.dates)
                discovery_dates = windows["discovery"]
                validation_dates = windows["validation"]
                hold_days = policy["hold_days"]
                validation_fwd = executable_forward_return(
                    panel, hold_days, boundary=config.VALIDATION_END,
                )
                discovery_fwd = executable_forward_return(
                    panel, hold_days, boundary=config.DISCOVERY_END,
                )
                result = run_validation(
                    factor, validation_fwd,
                    discovery_dates, validation_dates, hold_days,
                    panel=panel, exposures=None, is_fwd=discovery_fwd,
                    expected_direction=policy["expected_direction"],
                    apply_neutralization=policy["apply_neutralization"],
                )

            _reverify_blind_authorization(
                authorization_path, expected_sha, verification,
                verify_panel=True,
            )
            try:
                validation_p = float(result.get("validation_p_value", 1.0))
                if not math.isfinite(validation_p) or not 0.0 <= validation_p <= 1.0:
                    validation_p = 1.0
            except (TypeError, ValueError):
                validation_p = 1.0
            fdr_input = [{
                "factor_id": factor_id,
                "p_value": validation_p,
                "gate_passed": bool(result.get("passed")),
            }]
            fdr = l3_fdr(fdr_input)
            fdr_row = fdr.get(factor_id, {})
            passed = bool(
                result.get("passed")
                and result.get("stage") == "L3"
                and fdr_row.get("reject") is True
            )
            final_status = "validated_locked" if passed else "rejected"
            if passed:
                reason = "一次性盲态 L0-L3 通过；保持 validated_locked"
            elif result.get("stage") == "compute":
                reason = "compute: 因子计算失败或全为空"
            elif not result.get("passed"):
                stage = str(result.get("stage") or "validation")
                reason = f"{stage}: 科学验证未通过"
            else:
                reason = "L3-FDR 未通过"

            evidence = {
                "schema_version": 1,
                "stage": "blind_discovery_validation_once",
                "candidate_id": factor_id,
                "authorization_path": authorization_path,
                "authorization_sha256": expected_sha,
                "candidate_research_record_sha256": (
                    candidate.research_record_sha256
                ),
                "implementation_identity_sha256": reservation[
                    "implementation_identity_sha256"
                ],
                "implementation_audit_sha256": reservation[
                    "implementation_audit_sha256"
                ],
                "evaluator_fingerprint": reservation["evaluator_fingerprint"],
                "panel": {
                    "path": reservation["panel_path"],
                    "key": reservation["panel_key"],
                    "sha256": reservation["panel_sha256"],
                    "data_key": context["panel_data_key"],
                },
                "recipe": to_recipe(candidate),
                "validation_policy": policy,
                "result": result,
                "l3_fdr": fdr_row,
                "final_status": final_status,
                "production_unlocked": False,
            }
            evidence_path, evidence_sha256 = _write_blind_run_evidence(
                expected_sha, evidence,
            )

            registry_result = {
                **result,
                "status": final_status,
                "status_reason": reason,
                "p_value": validation_p,
                "research_epoch": verification["authorization"]["evaluator"][
                    "research_epoch"
                ],
                "validation_data_key": context["panel_data_key"],
            }
            reg.set_validation(connection, factor_id, registry_result)
            if passed:
                _seal_validated_factor(
                    connection, panel, factor_id, candidate,
                    evidence=registry_result,
                    sealed_at=claim.get("reserved_at"),
                    sealed_data_end=str(pd.Timestamp(max(panel.dates)).date()),
                    blind_authorization_sha256=expected_sha,
                )
            connection.commit()
            completed = reg.complete_blind_run(
                connection, expected_sha, context=context,
                evidence_path=evidence_path, evidence_sha256=evidence_sha256,
            )
            return {
                "ok": True,
                "candidate_id": factor_id,
                "authorization_sha256": expected_sha,
                "state": completed["state"],
                "replayed": False,
                "passed": passed,
                "final_status": final_status,
                "result": _json_safe(result),
                "l3_fdr": _json_safe(fdr_row),
                "evidence_path": evidence_path,
                "evidence_sha256": evidence_sha256,
                "production_unlocked": False,
            }
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            current = reg.blind_run_for_candidate(
                connection, factor_id or candidate_id or "",
            )
            if (
                owns_active_authorization
                and current
                and current.get("authorization_sha256") == expected_sha
                and current.get("state") in {"reserved", "running"}
            ):
                if factor_id is not None:
                    try:
                        reg.set_status(
                            connection, factor_id, "blind_unknown_locked",
                            "blind run 未完整冻结，结果按 unknown 锁定: "
                            f"{type(exc).__name__}",
                        )
                        connection.commit()
                    except Exception:
                        connection.rollback()
                reg.consume_blind_run_unknown(
                    connection, current["authorization_sha256"],
                    context=current["context"],
                    reason=f"blind run 异常，结果未知: {type(exc).__name__}",
                )
            raise
        finally:
            connection.close()
    finally:
        try:
            fcntl.flock(mutation_lock.fileno(), fcntl.LOCK_UN)
        finally:
            mutation_lock.close()


_MINING_COUNT_KEYS = (
    "compute_fail", "l0_fail", "l1_fail", "l2_fail", "dup_ic_fail",
    "incr_fail", "novelty_reject", "exploration_only",
)


def _mining_candidate_record(cand: Candidate) -> dict:
    """只冻结重建候选所需字段；callable 不进账本。"""
    reg.verify_candidate_source(cand)
    factor_id = str(getattr(cand, "factor_id", getattr(cand, "formula", "")))
    params = getattr(cand, "params", {})
    normalized = Candidate(
        factor_id=factor_id,
        name=str(getattr(cand, "name", factor_id)),
        family=str(getattr(cand, "family", "")),
        formula=str(getattr(cand, "formula", "")),
        description=str(getattr(cand, "description", "")),
        direction=str(getattr(cand, "direction", "")),
        data_requires=tuple(getattr(cand, "data_requires", ())),
        kind=str(getattr(cand, "kind", "")),
        compute=getattr(cand, "compute", lambda _panel: None),
        params=params if isinstance(params, dict) else {},
        research_record=_json_safe(getattr(cand, "research_record", {})),
        research_record_sha256=str(
            getattr(cand, "research_record_sha256", "") or ""
        ),
    )
    return {
        "candidate": {
            "factor_id": normalized.factor_id,
            "name": normalized.name,
            "family": normalized.family,
            "formula": normalized.formula,
            "description": normalized.description,
            "direction": normalized.direction,
            "data_requires": list(normalized.data_requires),
            "kind": normalized.kind,
            "params": _json_safe(normalized.params),
            "research_record": normalized.research_record,
            "research_record_sha256": normalized.research_record_sha256,
        },
        "signature": list(reg.candidate_signature(normalized)),
        "recipe": _json_safe(to_recipe(normalized)),
    }


def _candidate_from_mining_record(record: dict,
                                  atom_frames: dict | None = None) -> Candidate:
    """从不可变 family 恢复候选；计算也只消费账本内 recipe。"""
    spec = record.get("candidate")
    recipe = record.get("recipe")
    if not isinstance(spec, dict) or not isinstance(recipe, dict):
        raise RuntimeError("mining batch candidate/recipe 结构损坏")
    params = spec.get("params")
    requires = spec.get("data_requires")
    research_record = spec.get("research_record")
    research_sha256 = spec.get("research_record_sha256")
    if (not isinstance(params, dict) or not isinstance(requires, list)
            or not isinstance(research_record, dict) or not research_record
            or not isinstance(research_sha256, str) or not research_sha256):
        raise RuntimeError("mining batch candidate 参数结构损坏")
    cand = Candidate(
        factor_id=str(spec.get("factor_id", "")),
        name=str(spec.get("name", "")),
        family=str(spec.get("family", "")),
        formula=str(spec.get("formula", "")),
        description=str(spec.get("description", "")),
        direction=str(spec.get("direction", "")),
        data_requires=tuple(str(value) for value in requires),
        kind=str(spec.get("kind", "")),
        compute=lambda panel, frozen=recipe: rebuild_factor(
            panel, frozen, atom_frames=atom_frames,
        ),
        params=params,
        research_record=research_record,
        research_record_sha256=research_sha256,
    )
    if (_json_safe(to_recipe(cand)) != recipe
            or list(reg.candidate_signature(cand)) != record.get("signature")):
        raise RuntimeError(f"mining batch candidate 身份校验失败: {cand.factor_id}")
    reg.verify_candidate_source(cand)
    return cand


def _candidate_validation_policy(cand: Candidate) -> dict:
    """从可重建 recipe 读取候选验证口径；旧配方保持原默认。"""
    recipe = to_recipe(cand)
    hold_days = reg.recipe_execution_hold_days(recipe)
    validation_spec = recipe.get("validation_spec")
    if validation_spec is None:
        return {
            "hold_days": hold_days,
            "expected_direction": None,
            "apply_neutralization": True,
        }
    if not isinstance(validation_spec, dict):
        raise ValueError(
            f"[candidate_validation_spec_invalid] {cand.factor_id} 验证口径格式无效"
        )
    expected_direction = validation_spec.get("expected_direction")
    apply_neutralization = validation_spec.get("apply_neutralization")
    if (isinstance(expected_direction, bool)
            or not isinstance(expected_direction, (int, float))
            or not math.isfinite(float(expected_direction))
            or float(expected_direction) not in (-1.0, 1.0)
            or not isinstance(apply_neutralization, bool)):
        raise ValueError(
            f"[candidate_validation_spec_invalid] {cand.factor_id} 验证口径值无效"
        )
    return {
        "hold_days": hold_days,
        "expected_direction": float(expected_direction),
        "apply_neutralization": apply_neutralization,
    }


def _mining_counts(progress: dict) -> dict:
    counts = {key: 0 for key in _MINING_COUNT_KEYS}
    for record in progress.values():
        if record.get("state") != "terminal":
            continue
        for key, value in (record.get("count_deltas") or {}).items():
            counts[key] = counts.get(key, 0) + int(value)
    return counts


def _explored_on_input(c, data_key: str) -> set[tuple[str, str, str, str, str]]:
    """只跳过已在同一数据与标签实现快照上实际检验过的候选。"""
    rows = c.execute(
        "SELECT kind,family,formula,params_json,research_record_sha256 FROM factors "
        "WHERE research_epoch=? AND validation_data_key=?",
        (config.RESEARCH_EPOCH, data_key),
    ).fetchall()
    return {
        (str(row["kind"] or ""), str(row["family"] or ""),
         str(row["formula"] or ""), str(row["params_json"] or "{}"),
         str(row["research_record_sha256"] or ""))
        for row in rows
    }


def _novelty_decision(nov: dict) -> dict:
    return _json_safe({
        key: value for key, value in nov.items() if key != "ic"
    }) | {"has_ic": nov.get("ic") is not None}


def _reference_library_sha256(ref_lib: dict, dates: list[pd.Timestamp]) -> str:
    """冻结 prescreen 实际消费的 IC 面板与代表因子内容。"""
    digest = hashlib.sha256()

    def update_frame(label: str, frame: pd.DataFrame) -> None:
        normalized = pd.DataFrame(frame).reindex(index=dates)
        digest.update(label.encode("utf-8"))
        digest.update(json.dumps(
            [str(column) for column in normalized.columns],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        digest.update(json.dumps(
            [str(pd.Timestamp(index)) for index in normalized.index],
            ensure_ascii=False, separators=(",", ":"),
        ).encode("utf-8"))
        if not normalized.empty:
            row_hashes = pd.util.hash_pandas_object(
                normalized, index=True, categorize=True,
            ).to_numpy(dtype=np.uint64, copy=False)
            digest.update(row_hashes.tobytes())

    representatives = [str(value) for value in ref_lib.get("representatives", [])]
    digest.update(json.dumps(
        representatives, ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8"))
    update_frame("ic_panel", pd.DataFrame(ref_lib.get("ic_panel")))
    factors = ref_lib.get("factors") or {}
    for fid in representatives:
        frame = factors.get(fid)
        if not isinstance(frame, pd.DataFrame):
            digest.update(f"missing:{fid}".encode("utf-8"))
        else:
            update_frame(f"factor:{fid}", frame)
    return digest.hexdigest()


def _sync_novelty_decision(novelty, cand: Candidate, decision: dict,
                           factor: pd.DataFrame, is_dates: list,
                           is_fwd: pd.DataFrame, *, seed: int | None,
                           fitness=None) -> None:
    """ledger 先记决定，再幂等补齐外部 novelty 原子文件。"""
    if decision.get("has_ic"):
        ic = cross_sectional_ic(factor, is_fwd, is_dates)
        novelty.push_ic(cand.formula, ic)
    novelty.record(
        cand.formula, registered=False,
        fitness=decision.get("ic_ir") if fitness is None else fitness,
        ic_corr_nearest=decision.get("ic_corr_nearest"), seed=seed,
    )


def run_mining(quick: bool = False, include_genetic: bool = False,
               max_candidates: int = None, gen_seed: int | None = None,
               template_grid: bool = True,
               exposures: dict | None = None,
               novelty=None,
               atom_frames: dict | None = None,
               panel: Panel | None = None) -> dict:
    """挖候选→L0→L1→L2→L3(FDR)→入库。返回批次摘要。

    来源登记和预注册必须在任何 Panel/收益读取前通过。
    未经来源登记的 GP 参数仅保留为旧调用兼容面，运行时拒绝。
      exposures     ：预建中性化协变量（daemon 复用，避免每轮重建）
      novelty       ：NoveltyArchive 新颖性守卫（公式级跳过 + 信号级冗余拒）
      atom_frames   ：预建 GP 原子矩阵（daemon 跨轮复用，数据更新时整体丢弃）
    """
    if include_genetic:
        raise ValueError(
            "[unsourced_gp_disabled] 未经来源登记和预注册的 GP 候选已禁用"
        )
    batch_id = "mine-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    status.set_zone_status("mining", "running", phase="load", batch_id=batch_id)
    summary = {"batch_id": batch_id, "zone": "mining"}
    c = None
    try:
        catalog = source_gate.load_catalog(config.SOURCE_CATALOG)
        catalog_check = source_gate.verify_catalog(catalog)
        if not catalog_check["valid"]:
            raise ValueError(
                "[source_catalog_invalid] " + ",".join(catalog_check["errors"])
            )
        locked_source_candidate_ids = {
            str(item.get("candidate_id"))
            for item in catalog.get("candidates") or []
            if isinstance(item, dict)
            and source_gate.verify_candidate(item, catalog)["runnable"] is False
        }
        locked_source_candidate_ids.update(_blind_only_source_candidate_ids())
        reg.migrate_schema()
        c = reg._conn()
        pending = reg.pending_mining_batch(c, epoch=config.RESEARCH_EPOCH)
        resumed = pending is not None
        missing_implementation_ids: list[str] = []
        if resumed:
            declared = [
                _candidate_from_mining_record(record, atom_frames)
                for record in pending["family"]
            ]
            if any(
                cand.factor_id in locked_source_candidate_ids
                for cand in declared
            ):
                raise RuntimeError(
                    "[blind_candidate_generic_forbidden] pending 普通挖掘批包含"
                    "仅限一次性盲测的候选"
                )
            approved_candidates = list(declared)
            preflight_errors = []
        else:
            local_candidates = (
                generate_candidates(
                    None, [], None, include_genetic=False, quick=quick,
                )
                if template_grid else []
            )
            catalog_candidates = catalog.get("candidates")
            if isinstance(catalog_candidates, list):
                runnable_catalog_ids = {
                    str(item.get("candidate_id"))
                    for item in catalog_candidates
                    if isinstance(item, dict)
                    and source_gate.verify_candidate(item, catalog)["runnable"]
                }
                declared = [
                    cand for cand in local_candidates
                    if cand.factor_id in runnable_catalog_ids
                ]
                local_ids = {cand.factor_id for cand in local_candidates}
                missing_implementation_ids = sorted(
                    runnable_catalog_ids - local_ids
                )
            else:
                # Test/legacy injected catalogs without a candidates field keep
                # the old preflight path. A real catalog always has the field.
                declared = [
                    cand for cand in local_candidates
                    if cand.factor_id not in locked_source_candidate_ids
                ]
            preflight = source_gate.preflight_candidates(
                [cand.factor_id for cand in declared], catalog,
            )
            by_id = {
                result["candidate_id"]: result
                for result in preflight["results"]
            }
            approved_candidates = []
            for cand in declared:
                source = by_id.get(cand.factor_id)
                if source and source["runnable"]:
                    cand.research_record = source["research_record"]
                    cand.research_record_sha256 = source[
                        "research_record_sha256"
                    ]
                    reg.verify_candidate_source(cand)
                    approved_candidates.append(cand)
            preflight_errors = list(preflight["errors"])
            preflight_errors.extend(
                f"{factor_id}:local_implementation_missing"
                for factor_id in missing_implementation_ids
            )
        source_summary = {
            **source_gate.catalog_summary(catalog),
            "declared_candidate_count": len(declared),
            "runnable_candidate_ids": [
                cand.factor_id for cand in approved_candidates
            ],
            "blocked_candidate_count": (
                len(declared) - len(approved_candidates)
                + len(missing_implementation_ids)
            ),
            "preflight_errors": preflight_errors[:20],
        }
        summary["source_gate"] = source_summary
        if not approved_candidates:
            detail = (
                "[source_record_missing] 当前无已通过来源、PIT 与预注册"
                "门禁的可运行候选"
            )
            summary.update({
                "ok": True,
                "blocked": True,
                "status": "source_blocked",
                "phase": "source_gate",
                "reason": detail,
                "research_epoch": config.RESEARCH_EPOCH,
                "n_candidates": 0,
                "validated": 0,
                "n_rejected": 0,
            })
            status.set_zone_status(
                "mining", "blocked", phase="source_gate", batch_id=batch_id,
                queue_depth=0, detail=detail,
            )
            return summary
        approved_policies = {
            cand.factor_id: _candidate_validation_policy(cand)
            for cand in approved_candidates
        }
        hold_days_set = {
            policy["hold_days"] for policy in approved_policies.values()
        }
        if len(hold_days_set) != 1:
            raise ValueError(
                "[mixed_hold_days_forbidden] 同一挖掘批候选必须使用唯一持有期: "
                f"{sorted(hold_days_set)}"
            )
        hold_days = hold_days_set.pop()
        panel = panel if panel is not None else load_panel()
        windows = split_research_windows(panel.dates)
        is_dates = windows["discovery"]
        validation_dates = windows["validation"]
        historical_test_dates = windows["historical_test"]
        # 24h 搜索只看 discovery，L2 只看 validation；两段尾部都 purge。
        # 2024-07~2025-12 historical_test 在 mining 进程内根本不构造标签，
        # 杜绝连续 GP 把最终历史确认集变成调参集。
        summary["n_stocks"] = len(panel.codes)
        summary["n_is_dates"] = len(is_dates)
        summary["n_validation_dates"] = len(validation_dates)
        summary["n_oos_dates"] = len(validation_dates)  # 兼容旧看板字段
        summary["n_historical_test_dates"] = len(historical_test_dates)
        summary["research_epoch"] = config.RESEARCH_EPOCH
        summary["forward_return_label"] = EXECUTABLE_FORWARD_LABEL

        current_data_key = _panel_cache_path().name
        data_key = getattr(panel, "_panel_cache_key", None)
        if not data_key:
            raise RuntimeError("loaded Panel 缺少实际数据快照 key，拒绝挖掘")
        recovery_reject_reason = None
        if not resumed and data_key != current_data_key:
            raise RuntimeError(
                "数据源在 Panel 加载后已变化，拒绝把旧 Panel 贴成新快照: "
                f"{data_key} != {current_data_key}"
            )
        if resumed:
            if pending["data_key"] != data_key:
                loaded_data_key = data_key
                # 正常数据更新不能把 24h 机器永久卡在旧 pending。旧快照若
                # 已不可原样重演，唯一诚实且保守的恢复是冻结原 family，给所有
                # 未完成假设 p=1，并让已完成结果也失去 gate 资格；随后完整结算
                # 该族，绝不换新数据重算、缩小 family 或删除 pending。
                recovery_reject_reason = (
                    "pending mining batch 输入快照已变化，无法证明可原样重演；"
                    "整族保守按 gate=False 终结: "
                    f"{pending['data_key']} -> {loaded_data_key}"
                )
                data_key = pending["data_key"]
            batch_id = pending["batch_id"]
            summary["batch_id"] = batch_id
            context = pending["context"]
            if bool(context.get("novelty_enabled")) != bool(novelty is not None):
                recovery_reject_reason = (
                    "pending mining batch 的 novelty 配置已变化，无法证明可原样重演；"
                    "整族保守按 gate=False 终结"
                )
            if context.get("forward_return_label") != EXECUTABLE_FORWARD_LABEL:
                recovery_reject_reason = (
                    "pending mining batch 的收益标签口径已变化，无法证明可原样重演；"
                    "整族保守按 gate=False 终结"
                )
            cands = [
                _candidate_from_mining_record(record, atom_frames)
                for record in pending["family"]
            ]
            search_stats = context.get("search") or {}
            summary["already_explored"] = int(context.get("already_explored", 0))
            status.set_zone_status(
                "mining", "running", phase="recover", batch_id=batch_id,
                detail="恢复未完成候选族",
            )
        else:
            status.set_zone_status("mining", "running", phase="search",
                                   batch_id=batch_id, detail="生成候选")
            search_stats = {
                "source_declared": len(declared),
                "source_runnable": len(approved_candidates),
                "source_blocked": len(declared) - len(approved_candidates),
            }
            cands = list(approved_candidates)
            # Panel key 绑定 data.py 源码哈希；标签从 close-to-close 切换到
            # 可执行 open-to-open 后，旧 validation_data_key 不得继续冒充已探索。
            explored = _explored_on_input(c, data_key)
            before_skip = len(cands)
            cands = [
                cand for cand in cands
                if reg.candidate_trial_signature(cand) not in explored
            ]
            summary["already_explored"] = before_skip - len(cands)
            if max_candidates:
                cands = cands[:max_candidates]

        candidate_policies = {
            cand.factor_id: _candidate_validation_policy(cand) for cand in cands
        }
        if any(
            policy["hold_days"] != hold_days
            for policy in candidate_policies.values()
        ):
            raise ValueError("[mixed_hold_days_forbidden] 候选持有期在批内发生漂移")
        expected_directions = {
            factor_id: policy["expected_direction"]
            for factor_id, policy in candidate_policies.items()
        }
        neutralization_policies = {
            factor_id: policy["apply_neutralization"]
            for factor_id, policy in candidate_policies.items()
        }
        evaluator_fingerprint = _PROCESS_MINING_EVALUATOR_FINGERPRINT
        summary["hold_days"] = hold_days
        summary["expected_directions"] = expected_directions
        summary["apply_neutralization"] = neutralization_policies
        summary["evaluator_fingerprint"] = evaluator_fingerprint
        if resumed and (
            context.get("hold_days") != hold_days
            or context.get("expected_directions") != expected_directions
            or context.get("apply_neutralization") != neutralization_policies
            or context.get("evaluator_fingerprint") != evaluator_fingerprint
        ):
            recovery_reject_reason = (
                "pending mining batch 的持有期/方向/中性化或 evaluator 身份已变化，"
                "无法证明可原样重演；整族保守按 gate=False 终结"
            )

        if recovery_reject_reason:
            validation_fwd = None
            is_fwd = None
            summary["neutralize_mode"] = context.get(
                "neutralize_mode", "恢复漂移，未重算"
            )
        else:
            validation_fwd = executable_forward_return(
                panel, hold_days, boundary=config.VALIDATION_END,
            )
            is_fwd = executable_forward_return(
                panel, hold_days, boundary=config.DISCOVERY_END,
            )
            # 旧候选继续共用批级协变量；明确关闭中性化的候选不会构建或消费它。
            if any(neutralization_policies.values()) and exposures is None:
                exposures = build_exposures(panel)
            summary["neutralize_mode"] = neutralize_mode_label(
                exposures.get("mode")
                if any(neutralization_policies.values()) and exposures is not None
                else None
            )

        # 一批固定同一参考库；恢复时引用集合变化即停止，避免未完成候选换口径。
        if recovery_reject_reason:
            ref_ids = list(pending["context"].get("reference_ids", []))
            ref_representatives = list(
                pending["context"].get("reference_representatives", [])
            )
            reference_content_sha256 = pending["context"].get(
                "reference_content_sha256"
            )
            ref_lib = {
                "ic_panel": pd.DataFrame(columns=ref_ids),
                "representatives": ref_representatives,
                "factors": {},
            }
        else:
            ref_kwargs = ({"atom_frames": atom_frames}
                          if atom_frames is not None else {})
            ref_lib = build_reference_library(
                panel, is_dates, is_fwd, **ref_kwargs,
            )
            ref_ids = list(ref_lib["ic_panel"].columns)
            ref_representatives = list(ref_lib["representatives"])
            reference_content_sha256 = _reference_library_sha256(
                ref_lib, is_dates,
            )
            if resumed:
                if (ref_ids != pending["context"].get("reference_ids", [])
                        or ref_representatives
                        != pending["context"].get("reference_representatives", [])
                        or reference_content_sha256
                        != pending["context"].get("reference_content_sha256")):
                    recovery_reject_reason = (
                        "pending mining batch 参考因子集合/内容已变化，"
                        "无法证明可原样重演；"
                        "整族保守按 gate=False 终结"
                    )
            else:
                context = {
                    "already_explored": summary["already_explored"],
                    "gen_seed": gen_seed,
                    "novelty_enabled": novelty is not None,
                    "neutralize_mode": summary["neutralize_mode"],
                    "forward_return_label": EXECUTABLE_FORWARD_LABEL,
                    "hold_days": hold_days,
                    "expected_directions": expected_directions,
                    "apply_neutralization": neutralization_policies,
                    "evaluator_fingerprint": evaluator_fingerprint,
                    "reference_ids": ref_ids,
                    "reference_representatives": ref_representatives,
                    "reference_content_sha256": reference_content_sha256,
                    "search": _json_safe(search_stats),
                    "sealed_at": reg._now(),
                    "sealed_data_end": str(pd.Timestamp(max(panel.dates)).date()),
                }
                pending = reg.create_mining_batch(
                    c, batch_id=batch_id, epoch=config.RESEARCH_EPOCH,
                    data_key=data_key,
                    family=[_mining_candidate_record(cand) for cand in cands],
                    context=context,
                )
                c.commit()  # 完整候选族先于任何 explored/验证结果 durable

        total = len(cands)
        summary["n_candidates"] = total
        summary["search"] = search_stats
        summary["ref_factors"] = len(ref_ids)
        summary["ref_representatives"] = len(ref_representatives)
        progress = dict(pending["progress"])

        status.set_zone_status(
            "mining", "running", phase="validate", batch_id=batch_id,
            queue_depth=total - sum(
                row.get("state") == "terminal" for row in progress.values()
            ), detail="恢复 L0→L2" if resumed else "L0→L2 逐候选检验",
        )
        for position, cand in enumerate(cands):
            key = str(position)
            saved = progress.get(key)
            if saved is not None and saved.get("state") == "terminal":
                continue

            if recovery_reject_reason:
                fid = (str(saved.get("factor_id")) if isinstance(saved, dict)
                       and saved.get("factor_id") else reg.register_candidate(c, cand))
                row = c.execute(
                    "SELECT status FROM factors WHERE factor_id=?", (fid,),
                ).fetchone()
                cur = row["status"] if row else None
                # 若 novelty 决定已冻结但相关 IC 只能由旧快照重建，至少先把
                # 公式身份原子归档；不伪造/重算跨快照的信号指纹。
                if novelty is not None and cand.kind == "genetic":
                    novelty.record(
                        cand.formula, registered=False,
                        fitness=None, ic_corr_nearest=None,
                        seed=pending["context"].get("gen_seed"),
                    )
                conservative_result = {
                    "passed": False,
                    "stage": "L2",
                    "validation_p_value": 1.0,
                    "validation_p_value_method": "conservative_recovery_error_default",
                    "validation_p_value_n_effective": 0,
                    "L2": {"reason": recovery_reject_reason},
                    "recovery_error": recovery_reject_reason,
                }
                record = {
                    "state": "terminal",
                    "outcome": "validation_tested",
                    "factor_id": fid,
                    "validation_p_value": 1.0,
                    "validation_p_value_method": (
                        "conservative_recovery_error_default"
                    ),
                    "validation_p_value_n_effective": 0,
                    "gate_passed": False,
                    "gate_reason": recovery_reject_reason,
                    "result": conservative_result,
                    "incremental_ic": None,
                    "recovery_error": recovery_reject_reason,
                    "count_deltas": {"l2_fail": 1},
                }
                if isinstance(saved, dict) and saved.get("novelty") is not None:
                    record["novelty"] = saved["novelty"]
                _mining_reject(c, fid, cur, recovery_reject_reason)
                reg.mark_candidate_explored(
                    c, fid, epoch=config.RESEARCH_EPOCH, data_key=data_key,
                )
                reg.set_mining_candidate_progress(c, batch_id, position, record)
                c.commit()
                progress[key] = record
                continue

            fid = reg.register_candidate(c, cand)
            c.commit()  # 计算期间不持写锁；未有结果时崩溃会安全重算
            if saved is not None and saved.get("factor_id") != fid:
                raise RuntimeError(f"pending candidate factor_id 变化: {position}")
            row = c.execute(
                "SELECT status FROM factors WHERE factor_id=?", (fid,),
            ).fetchone()
            cur = row["status"] if row else None
            fr = compute_factor(panel, cand)
            if fr is None:
                record = {
                    "state": "terminal", "outcome": "excluded",
                    "factor_id": fid, "reason": "compute: 因子计算失败或全为空",
                    "count_deltas": {"compute_fail": 1},
                }
                _mining_reject(c, fid, cur, record["reason"])
                reg.mark_candidate_explored(
                    c, fid, epoch=config.RESEARCH_EPOCH, data_key=data_key,
                )
                reg.set_mining_candidate_progress(c, batch_id, position, record)
                c.commit()
                progress[key] = record
                continue

            decision = None
            if novelty is not None and cand.kind == "genetic":
                if saved is not None and saved.get("state") == "novelty_decided":
                    decision = saved.get("novelty")
                    if not isinstance(decision, dict):
                        raise RuntimeError(f"pending novelty 决定损坏: {position}")
                elif novelty.seen(cand.formula):
                    record = {
                        "state": "terminal", "outcome": "excluded",
                        "factor_id": fid, "reason": "新颖性归档：公式已探索",
                        "count_deltas": {"novelty_reject": 1},
                    }
                    _mining_reject(c, fid, cur, record["reason"])
                    reg.mark_candidate_explored(
                        c, fid, epoch=config.RESEARCH_EPOCH, data_key=data_key,
                    )
                    reg.set_mining_candidate_progress(c, batch_id, position, record)
                    c.commit()
                    progress[key] = record
                    del fr
                    continue
                else:
                    decision = _novelty_decision(
                        novelty.check_novelty(cand.formula, fr, is_dates, is_fwd)
                    )
                    saved = {
                        "state": "novelty_decided", "factor_id": fid,
                        "novelty": decision,
                    }
                    reg.set_mining_candidate_progress(c, batch_id, position, saved)
                    c.commit()  # 先冻结决定，archive 重试不会与自身指纹比较
                    progress[key] = saved
                _sync_novelty_decision(
                    novelty, cand, decision, fr, is_dates, is_fwd,
                    seed=pending["context"].get("gen_seed"),
                )
                if not decision.get("novel"):
                    record = {
                        "state": "terminal", "outcome": "excluded",
                        "factor_id": fid, "reason": "新颖性归档：IC 指纹高度重复",
                        "novelty": decision,
                        "count_deltas": {"novelty_reject": 1},
                    }
                    _mining_reject(c, fid, cur, record["reason"])
                    reg.mark_candidate_explored(
                        c, fid, epoch=config.RESEARCH_EPOCH, data_key=data_key,
                    )
                    reg.set_mining_candidate_progress(c, batch_id, position, record)
                    c.commit()
                    progress[key] = record
                    del fr
                    continue

            res = run_validation(
                fr, validation_fwd, is_dates, validation_dates,
                hold_days, panel=panel, exposures=exposures, is_fwd=is_fwd,
                expected_direction=candidate_policies[cand.factor_id][
                    "expected_direction"
                ],
                apply_neutralization=candidate_policies[cand.factor_id][
                    "apply_neutralization"
                ],
            )
            if decision is not None:
                _sync_novelty_decision(
                    novelty, cand, decision, fr, is_dates, is_fwd,
                    seed=pending["context"].get("gen_seed"),
                    fitness=res.get("is_ic_ir"),
                )

            stage = str(res["stage"])
            if (stage == "L1" and ref_lib["representatives"]
                    and candidate_policies[cand.factor_id][
                        "expected_direction"
                    ] is None):
                # 单因子晋升仍严格走 L1；但弱边际信号可能在控制已有因子后
                # 提供条件增量。此分支只保留为 research_only 组合假设，绝不
                # 读取 validation、进入 L3、seal 配方或排入历史/生产队列。
                try:
                    exploration_ok, exploration = prescreen_incremental(
                        fr, ref_lib["representatives"], ref_lib["factors"],
                        is_fwd, is_dates,
                        base_ic_mean=float(res.get("is_ic_mean") or 0.0),
                        require_base_direction=False,
                    )
                except Exception as exc:
                    exploration_ok = False
                    exploration = {
                        "ok": False,
                        "reason": f"条件增量探索异常({type(exc).__name__})",
                    }
                if exploration_ok:
                    reason = (
                        "单因子L1未晋升；条件增量通过，仅保留组合研究假设"
                    )
                    record = {
                        "state": "terminal", "outcome": "exploration_only",
                        "factor_id": fid, "reason": reason,
                        "result": _json_safe(res),
                        "exploration": _json_safe(exploration),
                        "incremental_ic": exploration.get("incremental_ic"),
                        "promotion_eligible": False,
                        "count_deltas": {"l1_fail": 1, "exploration_only": 1},
                    }
                    if decision is not None:
                        record["novelty"] = decision
                    reg.set_validation(c, fid, {
                        "status": "research_only",
                        "status_reason": reason,
                        "is_ic_mean": res.get("is_ic_mean"),
                        "is_ic_ir": res.get("is_ic_ir"),
                        "incremental_ic": exploration.get("incremental_ic"),
                        "research_epoch": config.RESEARCH_EPOCH,
                        "validation_data_key": data_key,
                    })
                    reg.mark_candidate_explored(
                        c, fid, epoch=config.RESEARCH_EPOCH, data_key=data_key,
                    )
                    reg.set_mining_candidate_progress(c, batch_id, position, record)
                    c.commit()
                    progress[key] = record
                    del fr
                    continue

            if stage in ("L0", "L1"):
                reason = res.get(stage, {}).get("reason", "未知")
                record = {
                    "state": "terminal", "outcome": "excluded",
                    "factor_id": fid, "reason": f"{stage}: {reason}",
                    "result": _json_safe(res),
                    "count_deltas": {stage.lower() + "_fail": 1},
                }
                if decision is not None:
                    record["novelty"] = decision
                _mining_reject(c, fid, cur, record["reason"])
                reg.mark_candidate_explored(
                    c, fid, epoch=config.RESEARCH_EPOCH, data_key=data_key,
                )
                reg.set_mining_candidate_progress(c, batch_id, position, record)
                c.commit()
                progress[key] = record
                del fr
                continue

            # L2 已读取独立 validation：无论 gate 成败都必须进入同一 BH family。
            try:
                validation_p = float(res.get("validation_p_value"))
                if not 0.0 <= validation_p <= 1.0 or not math.isfinite(validation_p):
                    validation_p = 1.0
            except (TypeError, ValueError):
                validation_p = 1.0
            gate_passed = bool(res.get("passed"))
            incr_ic = None
            count_deltas = {}
            gate_reason = ""
            if not gate_passed:
                count_deltas["l2_fail"] = 1
                gate_reason = f"L2: {res.get('L2', {}).get('reason', '未知')}"
            elif ref_lib["ic_panel"].shape[1] > 0:
                dup_reason = None
                try:
                    cand_ic = cross_sectional_ic(fr, is_fwd, is_dates)
                    ok_dup, dup_reason = prescreen_ic_correlation(
                        cand_ic, ref_lib["ic_panel"],
                    )
                    if ok_dup and ref_lib["representatives"]:
                        ok_inc, incr_info = prescreen_incremental(
                            fr, ref_lib["representatives"], ref_lib["factors"],
                            is_fwd, is_dates, base_ic_mean=res["is_ic_mean"],
                        )
                        incr_ic = incr_info.get("incremental_ic")
                        if not ok_inc:
                            count_deltas["incr_fail"] = 1
                            dup_reason = f"增量IC: {incr_info.get('reason')}"
                except Exception as exc:
                    dup_reason = f"预筛异常({type(exc).__name__})，fail-closed 拒"
                if dup_reason:
                    gate_passed = False
                    count_deltas["dup_ic_fail"] = 1
                    gate_reason = f"M1.4预筛: {dup_reason}"

            record = {
                "state": "terminal", "outcome": "validation_tested",
                "factor_id": fid, "validation_p_value": validation_p,
                "validation_p_value_method": res.get("validation_p_value_method"),
                "validation_p_value_n_effective": res.get(
                    "validation_p_value_n_effective"
                ),
                "gate_passed": gate_passed,
                "gate_reason": gate_reason,
                "result": _json_safe(res),
                "incremental_ic": _json_safe(incr_ic),
                "count_deltas": count_deltas,
            }
            if decision is not None:
                record["novelty"] = decision
            if not gate_passed:
                _mining_reject(c, fid, cur, gate_reason)
            reg.mark_candidate_explored(
                c, fid, epoch=config.RESEARCH_EPOCH, data_key=data_key,
            )
            reg.set_mining_candidate_progress(c, batch_id, position, record)
            c.commit()
            progress[key] = record
            del fr

            completed = sum(
                row.get("state") == "terminal" for row in progress.values()
            )
            if completed % 25 == 0 or completed == total:
                status.set_zone_status(
                    "mining", "running", phase="validate", batch_id=batch_id,
                    queue_depth=total - completed,
                    throughput=round(completed / max(1, _elapsed_s(summary)), 1),
                    detail=f"{completed}/{total}",
                )

        # ── L3：冻结所有实际读取 validation 的候选，不允许只保留 L2 winners ──
        counts = _mining_counts(progress)
        tested = []
        for position, cand in enumerate(cands):
            record = progress[str(position)]
            if record.get("outcome") == "validation_tested":
                gate_passed = (bool(record.get("gate_passed"))
                               and not recovery_reject_reason)
                tested.append({
                    "factor_id": record["factor_id"],
                    "p_value": record["validation_p_value"],
                    "p_value_method": record.get("validation_p_value_method"),
                    "p_value_n_effective": record.get(
                        "validation_p_value_n_effective"
                    ),
                    "gate_passed": gate_passed,
                    "gate_reason": (recovery_reject_reason
                                    or record.get("gate_reason") or ""),
                    "recovery_error": recovery_reject_reason,
                    "res": record["result"],
                    "cand": cand,
                    "incr_ic": record.get("incremental_ic"),
                })
        fdr_input = []
        for row in tested:
            frozen = {
                "factor_id": row["factor_id"],
                "p_value": row["p_value"],
                "p_value_method": row["p_value_method"],
                "p_value_n_effective": row["p_value_n_effective"],
                "gate_passed": row["gate_passed"],
            }
            if row.get("recovery_error"):
                frozen["recovery_error"] = row["recovery_error"]
            fdr_input.append(frozen)
        if recovery_reject_reason and pending.get("fdr_input") is not None:
            # FDR family 已在旧快照下冻结后才崩溃：不可重写其 p/gate。恢复
            # 只在最终动作层把全部候选保守降为 gate=False，并沿用原冻结族
            # 做可审计结算，避免 pending 永久死锁。
            frozen_fdr_input = pending["fdr_input"]
        else:
            pending = reg.freeze_mining_fdr_family(c, batch_id, fdr_input)
            c.commit()  # L3 前完整 validation family/p/gate 已 durable 且不可变
            frozen_fdr_input = pending["fdr_input"]
        counts["validation_tested"] = len(tested)
        counts["l2_pass"] = sum(row["gate_passed"] for row in tested)
        counts["l3_fail"] = 0
        counts["validated"] = 0
        fdr = l3_fdr(frozen_fdr_input)
        for row in tested:
            fid = row["factor_id"]
            r = fdr.get(fid, {})
            if row["gate_passed"] and r.get("reject", False):
                counts["validated"] += 1
                # 生命周期：先查原状态再更新指标，避免已投产/已冻结因子被降级重测
                existing = c.execute(
                    "SELECT status FROM factors WHERE factor_id=?", (fid,)).fetchone()
                cur = existing["status"] if existing else None
                # M1.3：分层单调 soft 提示写入原因（不否决）
                note = "L3-FDR 通过"
                _diag = row["res"].get("diag") or {}
                if _diag.get("monotonic", {}).get("monotonic_warn"):
                    note += "；分层单调偏弱(soft)"
                reg.set_validation(c, fid, {
                    "status": "validated",
                    "status_reason": note,
                    "incremental_ic": row.get("incr_ic"),
                    "research_epoch": config.RESEARCH_EPOCH,
                    "validation_data_key": data_key,
                    **row["res"],
                })
                recipe_evidence = {
                    **row["res"],
                    "research_epoch": config.RESEARCH_EPOCH,
                    "validation_data_key": data_key,
                }
                _seal_validated_factor(
                    c, panel, fid, row["cand"], evidence=recipe_evidence,
                    sealed_at=pending["context"]["sealed_at"],
                    sealed_data_end=pending["context"]["sealed_data_end"],
                )
                if cur in ("production", "frozen"):
                    reg.set_status(c, fid, cur, "重复挖掘命中，保留" + cur)
                else:
                    reg.set_status(c, fid, "registered", "L3 通过，待回测")
            else:
                if row["gate_passed"]:
                    counts["l3_fail"] += 1
                _exist = c.execute("SELECT status FROM factors WHERE factor_id=?", (fid,)).fetchone()
                reason = row["gate_reason"] if not row["gate_passed"] else (
                    "L3-FDR 未拒绝 "
                    f"(validation p={row['p_value']:.4f} > "
                    f"{r.get('p_adj_threshold', float('nan')):.4f})"
                )
                _mining_reject(
                    c, fid, _exist["status"] if _exist else None,
                    reason,
                )

        # M2.3 归档回填：genetic 因子最终 registry 状态（registered/production → 新颖成功）
        if novelty is not None:
            rows = c.execute(
                "SELECT formula, status FROM factors WHERE family='genetic'").fetchall()
            for r in rows:
                if (r["formula"] in novelty._data
                        and r["status"] in ("registered", "historical_pass", "production")):
                    novelty._data[r["formula"]]["registered"] = True
            # 去重归档是 24h 机器的正确性状态，不是可选日志；写失败必须停止本轮。
            novelty._save()

        reg.complete_mining_batch(c, batch_id)
        c.commit()
        c.close()
        c = None

        success_rate = counts["validated"] / total if total else 0.0
        summary.update(counts)
        summary["success_rate"] = round(success_rate, 4)
        summary.pop("_t0", None)
        if novelty is not None:
            summary["novelty_explored_total"] = novelty.total_explored()
            summary["novelty_registered_total"] = novelty.registered_count()
            summary["novelty_reject"] = counts["novelty_reject"]
        status.set_zone_status(
            "mining", "running", phase="done", batch_id=batch_id,
            queue_depth=0, throughput=0, detail="批处理完成")
        completed_metrics = status.mark_success(
            "mining", batch_id,
            detail=f"候选{total} 通过L3入库{counts['validated']}",
            success_rate=round(success_rate, 4),
            metrics={"candidates": total, "counts": counts,
                     "search": search_stats},
        ) or {}
        for key in ("funnel", "wall_seconds", "failure_feedback"):
            if key in completed_metrics:
                summary[key] = completed_metrics[key]
        summary["ok"] = True
        return summary
    except Exception as e:
        summary["ok"] = False
        summary["error"] = str(e)
        status.set_zone_status("mining", "error", phase="failed",
                               batch_id=batch_id, last_error=str(e),
                               detail="批处理异常")
        raise
    finally:
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
        gc.collect()


# ════════════════════════════════════════════════════════════════
# 回测区
# ════════════════════════════════════════════════════════════════


def _json_safe(value):
    """把 pandas/numpy 审计值收敛为严格 JSON；NaN/Inf 明确写 null。"""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, pd.Series):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, pd.DataFrame):
        return [_json_safe(r) for r in value.to_dict(orient="records")]
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if hasattr(value, "item"):
            value = value.item()
    except (TypeError, ValueError):
        pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_primary_signal_sample(fid: str, factor: pd.DataFrame,
                                 dates: list[pd.Timestamp], *,
                                 recipe_sha256: str, data_key: str,
                                 evaluator_fingerprint: str
                                 ) -> dict | None:
    """Freeze a small real evaluator output matrix for later independent replay."""
    if not isinstance(factor, pd.DataFrame):
        return None
    signal_dates = [pd.Timestamp(date) for date in dates[
        :int(config.FORWARD_SHADOW_MIN_DAYS)
    ]]
    if len(signal_dates) < int(config.FORWARD_SHADOW_MIN_DAYS):
        return None
    frame = factor.reindex(signal_dates).apply(pd.to_numeric, errors="coerce")
    columns = [column for column in frame.columns
               if np.isfinite(frame[column].to_numpy(dtype=float)).all()]
    by_name = {str(column): column for column in columns}
    if len(by_name) != len(columns) or len(by_name) < 30:
        return None
    names = sorted(by_name)[:64]
    values = frame[[by_name[name] for name in names]].to_numpy(dtype=float)
    if values.shape != (len(signal_dates), len(names)) or not np.isfinite(values).all():
        return None
    payload = {
        "schema_version": 1,
        "sample_type": "primary_factor_signal_sample",
        "factor_id": fid,
        "research_epoch": config.RESEARCH_EPOCH,
        "recipe_sha256": recipe_sha256,
        "data_key": data_key,
        "producer": {
            "type": "primary_evaluator",
            "evaluator_fingerprint": evaluator_fingerprint,
        },
        "dates": [str(date.date()) for date in signal_dates],
        "securities": names,
        "values": values.tolist(),
    }
    body = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ) + "\n").encode("utf-8")
    sha256 = hashlib.sha256(body).hexdigest()
    out_dir = (config.REGISTRY_DIR / "production_signal_samples" / "primary")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sha256}.json"
    if path.is_file():
        if _sha256_file(path) != sha256:
            raise RuntimeError("primary signal sample 内容寻址文件 hash 不符")
    else:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with tmp.open("wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(tmp, path)
            except FileExistsError:
                pass
        finally:
            tmp.unlink(missing_ok=True)
        if _sha256_file(path) != sha256:
            raise RuntimeError("primary signal sample 原子落盘后 hash 不符")
    return {
        "path": str(path.relative_to(config.PROJECT_ROOT)),
        "sha256": sha256,
    }


def _write_independent_reproduction_input(
    fid: str, panel: Panel, primary_binding: dict, *,
    recipe_sha256: str, data_key: str, evaluator_fingerprint: str,
) -> dict | None:
    """Freeze raw, pre-outcome Panel fields for a clean-room verifier."""
    try:
        primary_path = config.PROJECT_ROOT / primary_binding["path"]
        primary_raw = primary_path.read_bytes()
        if hashlib.sha256(primary_raw).hexdigest() != primary_binding["sha256"]:
            return None
        primary = json.loads(primary_raw.decode("utf-8"))
        signal_dates = [pd.Timestamp(value) for value in primary["dates"]]
        securities = [str(value) for value in primary["securities"]]
    except (KeyError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not signal_dates or len(securities) < 30:
        return None
    history_dates = [
        pd.Timestamp(value) for value in panel.dates
        if pd.Timestamp(value) <= signal_dates[-1]
    ]
    if not history_dates:
        return None

    frames: dict[str, list[list[object]]] = {}
    for name, value in sorted(vars(panel).items()):
        if not isinstance(value, pd.DataFrame):
            continue
        by_name = {str(column): column for column in value.columns}
        if len(by_name) != len(value.columns) or any(
            code not in by_name for code in securities
        ):
            continue
        matrix = value.reindex(
            index=history_dates,
            columns=[by_name[code] for code in securities],
        ).to_numpy(dtype=object).tolist()
        frames[name] = _json_safe(matrix)
    if not frames:
        return None
    payload = {
        "schema_version": 1,
        "input_type": "independent_factor_raw_input",
        "factor_id": fid,
        "research_epoch": config.RESEARCH_EPOCH,
        "recipe_sha256": recipe_sha256,
        "data_key": data_key,
        "producer": {
            "type": "primary_input_freezer",
            "evaluator_fingerprint": evaluator_fingerprint,
        },
        "signal_dates": [str(value.date()) for value in signal_dates],
        "history_dates": [str(value.date()) for value in history_dates],
        "securities": securities,
        "frames": frames,
    }
    body = (json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ) + "\n").encode("utf-8")
    sha256 = hashlib.sha256(body).hexdigest()
    out_dir = config.REGISTRY_DIR / "production_reproduction_inputs"
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sha256}.json"
    if path.is_file():
        if _sha256_file(path) != sha256:
            raise RuntimeError("independent reproduction input hash 不符")
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
        if _sha256_file(path) != sha256:
            raise RuntimeError("independent reproduction input 原子落盘失败")
    return {
        "path": str(path.relative_to(config.PROJECT_ROOT)),
        "sha256": sha256,
    }


def _write_factor_evidence(fid: str, stage: str, recipe_path, evidence: dict,
                           dates: list[pd.Timestamp], test_row: dict, *,
                           filename_suffix: str = "") -> tuple[str, str]:
    """原子落盘一份可哈希复核的因子终测证据；重启只接受原文件。"""
    existing_path = test_row.get("evidence_path")
    existing_sha = test_row.get("evidence_sha256")
    if existing_path and existing_sha and not filename_suffix:
        path = config.PROJECT_ROOT / existing_path
        if not path.is_file() or _sha256_file(path) != existing_sha:
            raise RuntimeError(f"已冻结证据缺失或哈希不符: {fid}/{stage}")
        return existing_path, existing_sha

    out_dir = config.REGISTRY_DIR / "evidence" / config.RESEARCH_EPOCH
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (
        f"{int(test_row['test_id']):06d}_{fid}_{stage}{filename_suffix}.json"
    )
    if recipe_path.is_file():
        recipe_sha = _sha256_file(recipe_path)
    else:
        recipe_sha = ((evidence.get("factor_seal") or {}).get("recipe_sha256")
                      if isinstance(evidence, dict) else None)
        if not recipe_sha:
            raise RuntimeError(f"因子配方缺失且无冻结哈希: {fid}/{stage}")
    payload = {
        "schema_version": 1,
        "factor_id": fid,
        "research_epoch": config.RESEARCH_EPOCH,
        "stage": stage,
        "evaluator_fingerprint": test_row.get("evaluator_fingerprint"),
        "tested_at": test_row.get("tested_at"),
        "data_key": str(test_row["data_key"]),
        "window": {
            "start": str(dates[0].date()) if dates else None,
            "end": str(dates[-1].date()) if dates else None,
            "n_dates": len(dates),
            "dates": [str(pd.Timestamp(date).date()) for date in dates],
        },
        "recipe": {
            "path": str(recipe_path.relative_to(config.PROJECT_ROOT)),
            "sha256": recipe_sha,
        },
        "online_fdr": {
            k: test_row.get(k) for k in (
                "epoch", "hypothesis_order", "p_value", "alpha_i",
                "gate_passed", "online_reject", "data_key", "p_value_method",
                "evaluator_fingerprint",
            )
        },
        "gate_evidence": evidence,
    }
    body = json.dumps(_json_safe(payload), ensure_ascii=False, indent=2,
                      sort_keys=True, allow_nan=False) + "\n"

    def accept_existing() -> tuple[str, str]:
        try:
            frozen = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"孤儿因子证据损坏: {fid}/{stage}") from exc
        identity_keys = (
            "schema_version", "factor_id", "research_epoch", "stage",
            "evaluator_fingerprint", "tested_at", "data_key", "window",
            "recipe", "online_fdr",
            "gate_evidence",
        )
        if any(frozen.get(key) != payload.get(key) for key in identity_keys):
            raise RuntimeError(f"孤儿因子证据与冻结检验不一致: {fid}/{stage}")
        rel = str(path.relative_to(config.PROJECT_ROOT))
        return rel, _sha256_file(path)

    if path.is_file():
        return accept_existing()

    # 临时文件写完并 fsync 后用 hard-link 原子占位；目标已存在时绝不覆盖，
    # 只按上面的冻结身份恢复。这样写文件→SQLite attach 之间崩溃仍可重试。
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as stream:
            stream.write(body.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return accept_existing()
    finally:
        tmp.unlink(missing_ok=True)
    rel = str(path.relative_to(config.PROJECT_ROOT))
    return rel, _sha256_file(path)


def _registry_evidence(evidence: dict, path: str, sha256: str,
                       test_row: dict) -> dict:
    """SQLite 只存摘要和账本定位，逐日大账本留在哈希文件。"""
    out = dict(evidence)
    for key in ("audit", "ledger", "daily_ledger", "signals", "trades"):
        out.pop(key, None)
    out["audit_evidence"] = {"path": path, "sha256": sha256}
    out["online_fdr"] = {
        k: test_row.get(k) for k in (
            "epoch", "hypothesis_order", "p_value", "alpha_i",
            "gate_passed", "online_reject", "data_key", "p_value_method",
            "evaluator_fingerprint",
        )
    }
    return out


def _recipe_hold_days_for_row(row: dict) -> int:
    frozen = row.get("_execution_hold_days")
    if frozen is not None:
        return int(frozen)
    path = config.REGISTRY_DIR / "recipes" / f"{row.get('factor_id', '')}.json"
    try:
        raw = path.read_bytes()
        if row.get("sealed_recipe_sha256") and hashlib.sha256(raw).hexdigest() != row[
            "sealed_recipe_sha256"
        ]:
            raise ValueError
        return reg.recipe_execution_hold_days(json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
        # Selection is only a cheap queue classification. The stage evaluator
        # validates the frozen recipe and invalidates malformed rows before data use.
        return int(config.HOLD_DAYS)


def _forward_window_after_seal(row: dict,
                               forward_dates: list[pd.Timestamp], *,
                               hold_days: int | None = None
                               ) -> list[pd.Timestamp]:
    """返回该假设 seal 后首个固定前向窗；不足完整标签数时返回空。"""
    sealed_end = row.get("evaluator_sealed_data_end") or row.get("sealed_data_end")
    if not sealed_end:
        return []
    boundary = pd.Timestamp(sealed_end)
    eligible = sorted(
        pd.Timestamp(d) for d in forward_dates if pd.Timestamp(d) > boundary
    )
    # T+1 开盘入场、T+H+1 开盘退出的标签需要尾部再留 H+1 个交易日，
    # 不能把尚未完成的
    # 标签计入“120 日已观察”。
    hold_days = (_recipe_hold_days_for_row(row) if hold_days is None
                 else int(hold_days))
    required = int(config.FORWARD_SHADOW_MIN_DAYS) + hold_days + 1
    return eligible[:required] if len(eligible) >= required else []


def _select_forward_work(rows: list[dict], forward_dates: list[pd.Timestamp],
                         *, batch_size: int,
                         pending_factor_id: str | None = None
                         ) -> tuple[list[tuple[dict, list[pd.Timestamp]]], int, int]:
    """全队列先做成熟分类，返回（本批成熟项、等待数、剩余成熟数）。"""
    eligible = []
    waiting = 0
    for row in rows:
        frozen = _forward_window_after_seal(row, forward_dates)
        if frozen:
            eligible.append((row, frozen))
        else:
            waiting += 1
    limit = max(1, batch_size) if pending_factor_id else max(0, batch_size)
    selected = eligible[:limit]
    return selected, waiting, max(0, len(eligible) - len(selected))


def _factor_observation_context(row: dict, stage: str,
                                dates: list[pd.Timestamp]) -> dict:
    return {
        "producer": "factor_backtest",
        "stage": stage,
        "research_epoch": config.RESEARCH_EPOCH,
        "factor_id": row["factor_id"],
        "sealed_data_end": row.get("sealed_data_end"),
        "sealed_recipe_sha256": row.get("sealed_recipe_sha256"),
        "evaluator_sealed_at": row.get("evaluator_sealed_at"),
        "evaluator_sealed_data_end": row.get("evaluator_sealed_data_end"),
        "evaluator_fingerprint": production.process_evaluator_fingerprint(),
        "window_start": str(pd.Timestamp(dates[0]).date()) if dates else None,
        "window_end": str(pd.Timestamp(dates[-1]).date()) if dates else None,
        "window_n_dates": len(dates),
        "execution_hold_days": _recipe_hold_days_for_row(row),
    }


_FACTOR_TEST_EVIDENCE_KEYS = (
    "epoch", "hypothesis_order", "p_value", "alpha_i", "gate_passed",
    "online_reject", "data_key", "p_value_method", "evaluator_fingerprint",
)


def _read_frozen_factor_evidence(test_row: dict, row: dict, *, stage: str
                                 ) -> tuple[dict | None, str | None]:
    """只接受与冻结 test row 完全同一身份、且哈希可复核的原始证据。"""
    rel_path = test_row.get("evidence_path")
    expected_sha = test_row.get("evidence_sha256")
    if not rel_path or not expected_sha:
        return None, "冻结检验缺少已附着证据"
    try:
        root = config.PROJECT_ROOT.resolve()
        path = (root / rel_path).resolve()
        path.relative_to(root)
        if not path.is_file():
            return None, "冻结检验的已附着证据文件缺失"
        body = path.read_bytes()
        if hashlib.sha256(body).hexdigest() != expected_sha:
            return None, "冻结检验的已附着证据哈希不符"
        payload = json.loads(body.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
        return None, "冻结检验的已附着证据无法安全读取"
    if not isinstance(payload, dict):
        return None, "冻结检验的已附着证据格式无效"
    expected_online = {
        key: test_row.get(key) for key in _FACTOR_TEST_EVIDENCE_KEYS
    }
    identity = {
        "schema_version": 1,
        "factor_id": row["factor_id"],
        "research_epoch": config.RESEARCH_EPOCH,
        "stage": stage,
        "tested_at": test_row.get("tested_at"),
        "data_key": str(test_row["data_key"]),
        "online_fdr": expected_online,
    }
    if stage == "forward_shadow":
        identity["evaluator_fingerprint"] = test_row.get("evaluator_fingerprint")
    if any(payload.get(key) != value for key, value in identity.items()):
        return None, "冻结检验的已附着证据身份与账本不一致"
    evidence = payload.get("gate_evidence")
    if not isinstance(evidence, dict):
        return None, "冻结检验的已附着证据缺少 gate audit"
    factor_seal = evidence.get("factor_seal") or {}
    if factor_seal != {
        "sealed_at": row.get("sealed_at"),
        "sealed_data_end": row.get("sealed_data_end"),
        "recipe_sha256": row.get("sealed_recipe_sha256"),
    }:
        return None, "冻结检验的已附着证据与因子 seal 不一致"
    if stage == "forward_shadow" and evidence.get("evaluator_seal") != {
        "sealed_at": row.get("evaluator_sealed_at"),
        "sealed_data_end": row.get("evaluator_sealed_data_end"),
        "evaluator_fingerprint": row.get("evaluator_fingerprint"),
    }:
        return None, "冻结检验的已附着证据与 evaluator seal 不一致"
    observed_p = (evidence.get("oos_ic") or {}).get("p_value")
    try:
        p_matches = abs(float(observed_p) - float(test_row["p_value"])) <= 1e-12
    except (TypeError, ValueError):
        p_matches = False
    if not p_matches:
        return None, "冻结检验的已附着证据 p 值与账本不一致"
    return evidence, None


def _factor_stage_decision(stage: str, test_row: dict,
                           evidence: dict) -> tuple[str, str]:
    passed = bool(test_row.get("gate_passed"))
    online_ok = bool(test_row.get("online_reject"))
    if stage == "historical_confirmation":
        if passed and online_ok:
            return (
                "historical_pass",
                "历史确认与全局在线 LOND 通过；仅为研究候选，等待 seal 后 "
                f"前向观察满 {config.FORWARD_SHADOW_MIN_DAYS} 日",
            )
        if not passed:
            return "rejected", evidence.get("reason", "历史科学闸门未通过")
        return "rejected", "历史科学闸门通过，但全局在线 LOND 未通过"
    if passed and online_ok:
        return "production", "seal 后冻结前向终测与全局在线 LOND 全部通过，正式投产"
    if not passed:
        return "rejected", evidence.get("reason", "前向科学闸门未通过")
    return "rejected", "前向科学闸门通过，但全局在线 LOND 未通过"


def _recover_frozen_factor_test(c, panel: Panel, row: dict, *, stage: str,
                                test_epoch: str, recipe_path) -> str | None:
    """终结 record 已提交的中断任务；绝不重新读取数据或运行 gate。"""
    frozen = c.execute(
        "SELECT * FROM factor_tests WHERE epoch=? AND factor_id=?",
        (test_epoch, row["factor_id"]),
    ).fetchone()
    if frozen is None:
        return None
    test_row = dict(frozen)
    evidence, invalid_reason = _read_frozen_factor_evidence(
        test_row, row, stage=stage,
    )
    if evidence is not None:
        recovery = evidence.get("recovery") or {}
        if recovery.get("conservative_final_status") == "rejected":
            final_status = "rejected"
            reason = evidence.get("reason", "冻结检验证据不完整，保守拒绝")
            effective_gate_passed = False
        else:
            final_status, reason = _factor_stage_decision(
                stage, test_row, evidence,
            )
            effective_gate_passed = bool(test_row.get("gate_passed"))
        path = str(test_row["evidence_path"])
        sha256 = str(test_row["evidence_sha256"])
        registry_ev = _registry_evidence(evidence, path, sha256, test_row)
        if final_status == "production":
            admission = c.execute(
                "SELECT production_admission_path,production_admission_sha256 "
                "FROM factors WHERE factor_id=?", (row["factor_id"],),
            ).fetchone()
            if admission is None or not all(admission):
                final_status = "historical_pass"
                reg.set_backtest(
                    c, row["factor_id"], final_status,
                    "promotion_pending：等待外部独立复现 admission certificate",
                    _gate_metrics(evidence), evidence=registry_ev,
                    backtest_epoch=test_epoch,
                    gate_passed=effective_gate_passed,
                    evidence_path=path, evidence_sha256=sha256,
                )
                c.commit()
                return final_status
            try:
                production.promote_factor(
                    c, panel, row["factor_id"], metrics=_gate_metrics(evidence),
                    registry_evidence=registry_ev, test_epoch=test_epoch,
                    evidence_path=path, evidence_sha256=sha256, reason=reason,
                )
            except Exception as exc:
                final_status = "historical_pass"
                retry_reason = (
                    "前向门禁已通过，但生产制品未能安全生成，等待重试: "
                    f"{exc}"
                )
                reg.set_backtest(
                    c, row["factor_id"], final_status,
                    retry_reason,
                    _gate_metrics(evidence), evidence=registry_ev,
                    backtest_epoch=test_epoch, gate_passed=effective_gate_passed,
                    evidence_path=path, evidence_sha256=sha256,
                )
                c.commit()
                raise RuntimeError(retry_reason) from exc
        else:
            evaluator_sealed_data_end = None
            if final_status == "historical_pass" and stage == "historical_confirmation":
                panel_dates = getattr(panel, "dates", None)
                if not panel_dates:
                    raise RuntimeError("historical_pass 缺少实际 Panel 最新日")
                evaluator_sealed_data_end = str(
                    pd.Timestamp(max(panel_dates)).date()
                )
            reg.set_backtest(
                c, row["factor_id"], final_status, reason, _gate_metrics(evidence),
                evidence=registry_ev, backtest_epoch=test_epoch,
                gate_passed=effective_gate_passed,
                evidence_path=path, evidence_sha256=sha256,
                evaluator_sealed_data_end=evaluator_sealed_data_end,
            )
            c.commit()
        return final_status

    reason = (
        "冻结 LOND 检验已提交，但完整 gate 证据未可靠落盘；"
        f"不使用新数据重演并保守拒绝: {invalid_reason}"
    )
    evidence = {
        "verdict": "ERROR",
        "reason": reason,
        "p_value_source": "frozen_factor_test_row_preserved",
        "new_data_used_for_decision": False,
        "oos_ic": {
            "p_value": test_row["p_value"],
            "p_value_method": test_row["p_value_method"],
        },
        "window_role": stage,
        "research_epoch": config.RESEARCH_EPOCH,
        "factor_seal": {
            "sealed_at": row.get("sealed_at"),
            "sealed_data_end": row.get("sealed_data_end"),
            "recipe_sha256": row.get("sealed_recipe_sha256"),
        },
        "recovery": {
            "kind": "frozen_factor_test_without_valid_evidence",
            "conservative_final_status": "rejected",
            "failure": invalid_reason,
            "test_id": test_row["test_id"],
            "hypothesis_order": test_row["hypothesis_order"],
        },
    }
    path = None
    sha256 = None
    try:
        path, sha256 = _write_factor_evidence(
            row["factor_id"], stage, recipe_path, evidence, [], test_row,
            filename_suffix="_recovery",
        )
        if not test_row.get("evidence_path") and not test_row.get("evidence_sha256"):
            reg.attach_factor_test_evidence(
                c, int(test_row["test_id"]), evidence_path=path,
                evidence_sha256=sha256,
            )
            c.commit()
    except Exception as exc:
        evidence["recovery_evidence_write_error"] = str(exc)
        path = None
        sha256 = None
    registry_ev = _registry_evidence(evidence, path, sha256, test_row)
    reg.set_backtest(
        c, row["factor_id"], "rejected", reason, {}, evidence=registry_ev,
        backtest_epoch=test_epoch, gate_passed=False,
        evidence_path=path, evidence_sha256=sha256,
    )
    c.commit()
    return "rejected"


def _evaluate_factor_stage(c, panel: Panel, row: dict, dates: list[pd.Timestamp],
                           *, stage: str, test_epoch: str) -> str:
    """冻结配方后执行一个独立时间段；返回最终状态。"""
    fid = row["factor_id"]
    recipe_path = config.REGISTRY_DIR / "recipes" / f"{fid}.json"
    if not recipe_path.exists():
        reg.set_status(c, fid, "rejected", "配方缺失，无法重建因子")
        return "rejected"
    recipe = json.loads(recipe_path.read_text("utf-8"))
    if recipe.get("research_epoch") != config.RESEARCH_EPOCH:
        reg.set_status(c, fid, "invalidated", "配方不属于当前严格 research epoch")
        return "invalidated"
    seal_keys = (
        "sealed_at", "sealed_data_end", "sealed_recipe_sha256",
        "research_record_json", "research_record_sha256",
    )
    if any(not row.get(key) for key in seal_keys):
        reg.set_status(c, fid, "invalidated", "当前 epoch 因子缺少不可变 seal")
        return "invalidated"
    if (_sha256_file(recipe_path) != row["sealed_recipe_sha256"]
            or recipe.get("sealed_at") != row["sealed_at"]
            or recipe.get("sealed_data_end") != row["sealed_data_end"]
            or recipe.get("research_record_sha256")
            != row.get("research_record_sha256")):
        reg.set_status(c, fid, "invalidated", "因子 seal/配方哈希校验失败")
        return "invalidated"
    try:
        reg.verify_factor_source_for_pipeline(c, row, recipe)
        hold_days = reg.recipe_execution_hold_days(recipe)
    except ValueError as exc:
        reg.set_status(c, fid, "invalidated", str(exc))
        return "invalidated"
    row = {**row, "_execution_hold_days": hold_days}
    if stage == "forward_shadow":
        try:
            reg.require_independent_verifier_commitment(c, row)
        except ValueError:
            reg.set_status(
                c, fid, "historical_pass",
                "等待前向观察前冻结的独立 clean-room verifier",
            )
            return "waiting"
        active_evaluator = production.process_evaluator_fingerprint()
        if row.get("evaluator_fingerprint") != active_evaluator:
            panel_dates = getattr(panel, "dates", None)
            if not panel_dates:
                raise RuntimeError("evaluator 漂移重置缺少实际 Panel 最新日")
            reg.reset_evaluator_seal(
                c, fid,
                sealed_data_end=str(pd.Timestamp(max(panel_dates)).date()),
                reason="evaluator 已变化；在当前 Panel 最新日重置前向观察起点",
            )
            return "historical_pass"
    recovered = _recover_frozen_factor_test(
        c, panel, row, stage=stage, test_epoch=test_epoch, recipe_path=recipe_path,
    )
    if recovered is not None:
        return recovered
    if stage == "forward_shadow":
        expected_dates = _forward_window_after_seal(
            row, dates, hold_days=hold_days,
        )
        normalized = [pd.Timestamp(d) for d in dates]
        # 调用方必须传入恰好一个固定窗；不能截短、滑窗或把 seal 前数据混入。
        if not expected_dates or normalized != expected_dates:
            reg.set_status(c, fid, "historical_pass",
                           "seal 后完整前向标签不足或窗口不匹配，继续等待")
            return "historical_pass"
    factor = rebuild_factor(panel, recipe)
    if factor is None:
        reg.set_status(c, fid, "rejected", "因子重建失败")
        return "rejected"
    is_direction = (1.0 if row.get("is_ic_mean", 0) >= 0 else -1.0) \
        if pd.notna(row.get("is_ic_mean")) else 1.0
    p_value_method = "non_overlapping_phase_max_hac"
    data_key = research_input_key(
        panel_key=getattr(panel, "_panel_cache_key", None),
    )
    observation_context = _factor_observation_context(row, stage, dates)
    # 先持久化假设到达顺序，再读取历史确认/前向数据。若更早假设
    # 尚未完成，begin 会 fail-closed，不会让本假设抢占更早 alpha。
    reg.begin_factor_observation(
        c, fid, epoch=test_epoch, p_value_method=p_value_method,
        data_key=data_key, producer_type="factor_backtest",
        context=observation_context,
    )
    c.commit()
    try:
        passed, evidence = run_scientific_gate(
            panel, factor, dates, hold=hold_days, q=config.QUANTILE,
            is_direction=is_direction,
            capture_audit=True,
            expected_input_key=data_key,
        )
    except Exception as exc:
        # gate 可能已在内部算出/观察到历史 p 后才于折检验、敏感性或审计阶段
        # 抛错。无法证明“尚未观察”时必须保守消费当前序号，不能让后续假设
        # 因异常而拿到更早、更宽松的 alpha。
        del factor
        error_evidence = {
            "verdict": "ERROR",
            "reason": f"科学闸门评估异常，保守按 p=1 拒绝: {exc}",
            "evaluation_error": str(exc),
            "p_value_source": "conservative_evaluation_error_default",
            "oos_ic": {
                "p_value": 1.0,
                "p_value_method": "non_overlapping_phase_max_hac",
            },
            "window_role": stage,
            "research_epoch": config.RESEARCH_EPOCH,
            "factor_seal": {
                "sealed_at": row["sealed_at"],
                "sealed_data_end": row["sealed_data_end"],
                "recipe_sha256": row["sealed_recipe_sha256"],
            },
        }
        test_row = reg.record_factor_test(
            c, fid, epoch=test_epoch, p_value=1.0, gate_passed=False,
            p_value_method=p_value_method, data_key=data_key,
            require_observation=True,
            producer_type="factor_backtest", context=observation_context,
        )
        c.commit()
        path, sha256 = _write_factor_evidence(
            fid, stage, recipe_path, error_evidence, dates, test_row,
        )
        reg.attach_factor_test_evidence(
            c, int(test_row["test_id"]), evidence_path=path,
            evidence_sha256=sha256,
        )
        c.commit()
        registry_ev = _registry_evidence(
            error_evidence, path, sha256, test_row,
        )
        reg.set_backtest(
            c, fid, "rejected", error_evidence["reason"], {},
            evidence=registry_ev, backtest_epoch=test_epoch,
            gate_passed=False, evidence_path=path, evidence_sha256=sha256,
        )
        return "rejected"
    evidence = dict(evidence)
    if stage == "forward_shadow" and passed:
        try:
            primary_sample = _write_primary_signal_sample(
                fid, factor, dates,
                recipe_sha256=row["sealed_recipe_sha256"],
                data_key=data_key,
                evaluator_fingerprint=(
                    row.get("evaluator_fingerprint")
                    or production.process_evaluator_fingerprint()
                ),
            )
            if primary_sample is not None:
                evidence["primary_signal_sample"] = primary_sample
                independent_input = _write_independent_reproduction_input(
                    fid, panel, primary_sample,
                    recipe_sha256=row["sealed_recipe_sha256"],
                    data_key=data_key,
                    evaluator_fingerprint=(
                        row.get("evaluator_fingerprint")
                        or production.process_evaluator_fingerprint()
                    ),
                )
                if independent_input is not None:
                    evidence["independent_reproduction_input"] = independent_input
                else:
                    evidence["independent_reproduction_input_error"] = (
                        "冻结 raw Panel 复现输入不可用"
                    )
            else:
                evidence["primary_signal_sample_error"] = (
                    "完整120日×30证券 primary signal sample 不可用"
                )
        except Exception as exc:
            evidence["primary_signal_sample_error"] = str(exc)
    del factor
    evidence["window_role"] = stage
    evidence["research_epoch"] = config.RESEARCH_EPOCH
    evidence["factor_seal"] = {
        "sealed_at": row["sealed_at"],
        "sealed_data_end": row["sealed_data_end"],
        "recipe_sha256": row["sealed_recipe_sha256"],
    }
    evidence["evaluator_seal"] = {
        "sealed_at": row.get("evaluator_sealed_at") or row["sealed_at"],
        "sealed_data_end": (
            row.get("evaluator_sealed_data_end") or row["sealed_data_end"]
        ),
        "evaluator_fingerprint": (
            row.get("evaluator_fingerprint")
            or production.process_evaluator_fingerprint()
        ),
    }
    p_value = (evidence.get("oos_ic") or {}).get("p_value", 1.0)
    observed_method = ((evidence.get("oos_ic") or {}).get("p_value_method")
                       or p_value_method)
    test_row = reg.record_factor_test(
        c, fid, epoch=test_epoch, p_value=p_value, gate_passed=passed,
        p_value_method=observed_method, data_key=data_key,
        require_observation=True,
        producer_type="factor_backtest", context=observation_context,
    )
    # LOND 顺序与判定必须先独立持久化；后续证据 I/O 或进程崩溃不能回滚
    # 已经观察到的假设，也不能让它在恢复后重新消费显著性预算。
    c.commit()
    evidence["online_fdr"] = {
        k: test_row.get(k) for k in (
            "epoch", "hypothesis_order", "p_value", "alpha_i",
            "gate_passed", "online_reject", "data_key", "p_value_method",
            "evaluator_fingerprint",
        )
    }
    path, sha256 = _write_factor_evidence(
        fid, stage, recipe_path, evidence, dates, test_row,
    )
    reg.attach_factor_test_evidence(
        c, int(test_row["test_id"]), evidence_path=path, evidence_sha256=sha256,
    )
    c.commit()
    registry_ev = _registry_evidence(evidence, path, sha256, test_row)
    metrics = _gate_metrics(evidence)
    final_status, reason = _factor_stage_decision(stage, test_row, evidence)
    if final_status == "production":
        admission = c.execute(
            "SELECT production_admission_path,production_admission_sha256 "
            "FROM factors WHERE factor_id=?", (fid,),
        ).fetchone()
        if admission is None or not all(admission):
            final_status = "historical_pass"
            reg.set_backtest(
                c, fid, final_status,
                "promotion_pending：等待外部独立复现 admission certificate",
                metrics, evidence=registry_ev, backtest_epoch=test_epoch,
                gate_passed=passed, evidence_path=path,
                evidence_sha256=sha256,
            )
            c.commit()
            return final_status
        try:
            production.promote_factor(
                c, panel, fid, metrics=metrics,
                registry_evidence=registry_ev, test_epoch=test_epoch,
                evidence_path=path, evidence_sha256=sha256, reason=reason,
            )
        except Exception as exc:
            final_status = "historical_pass"
            retry_reason = (
                "前向门禁已通过，但生产制品未能安全生成，等待重试: "
                f"{exc}"
            )
            reg.set_backtest(
                c, fid, final_status,
                retry_reason,
                metrics, evidence=registry_ev, backtest_epoch=test_epoch,
                gate_passed=passed, evidence_path=path,
                evidence_sha256=sha256,
            )
            c.commit()
            raise RuntimeError(retry_reason) from exc
    else:
        evaluator_sealed_data_end = None
        if final_status == "historical_pass" and stage == "historical_confirmation":
            panel_dates = getattr(panel, "dates", None)
            if not panel_dates:
                raise RuntimeError("historical_pass 缺少实际 Panel 最新日")
            evaluator_sealed_data_end = str(
                pd.Timestamp(max(panel_dates)).date()
            )
        reg.set_backtest(
            c, fid, final_status, reason, metrics, evidence=registry_ev,
            backtest_epoch=test_epoch, gate_passed=passed,
            evidence_path=path, evidence_sha256=sha256,
            evaluator_sealed_data_end=evaluator_sealed_data_end,
        )
    return final_status


def _pending_factor_row(c, latch: dict, *, stage: str) -> tuple[dict, dict]:
    """将 pending latch 严格路由回原因子任务，禁止跨 producer 猜测恢复。"""
    if latch.get("producer_type") != "factor_backtest":
        raise RuntimeError(
            f"{latch['epoch']} 有待恢复 {latch.get('producer_type')} 假设 "
            f"{latch['factor_id']}；因子 daemon 不得越过，请由原生产者恢复"
        )
    try:
        context = json.loads(latch.get("context_json") or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"pending 观察上下文损坏: {latch['factor_id']}") from exc
    canonical = json.dumps(
        context, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    if hashlib.sha256(canonical.encode("utf-8")).hexdigest() != latch.get(
        "context_sha256"
    ):
        raise RuntimeError(f"pending 观察上下文哈希损坏: {latch['factor_id']}")
    if (context.get("producer") != "factor_backtest"
            or context.get("stage") != stage
            or context.get("research_epoch") != config.RESEARCH_EPOCH
            or context.get("factor_id") != latch["factor_id"]):
        raise RuntimeError(
            f"pending 观察恢复上下文不匹配: {latch['factor_id']}/{stage}"
        )
    if context.get("evaluator_fingerprint") != latch.get("evaluator_fingerprint"):
        raise RuntimeError(
            f"pending 观察 evaluator 身份损坏: {latch['factor_id']}"
        )
    row = c.execute(
        "SELECT * FROM factors WHERE factor_id=? AND research_epoch=?",
        (latch["factor_id"], config.RESEARCH_EPOCH),
    ).fetchone()
    if row is None:
        raise RuntimeError(
            f"pending 观察的因子不存在或 epoch 不匹配: {latch['factor_id']}"
        )
    row = dict(row)
    if (context.get("sealed_data_end") != row.get("sealed_data_end")
            or context.get("sealed_recipe_sha256")
            != row.get("sealed_recipe_sha256")):
        raise RuntimeError(f"pending 观察的因子 seal 已变化: {latch['factor_id']}")
    return row, context


def _pending_replay_error(row: dict, latch: dict, *, stage: str,
                          dates: list[pd.Timestamp], current_data_key: str,
                          test_epoch: dict | None = None) -> str | None:
    if latch["data_key"] != current_data_key:
        return f"输入快照已变化: {latch['data_key']} -> {current_data_key}"
    if latch.get("evaluator_fingerprint") != production.process_evaluator_fingerprint():
        return "预约 evaluator 与当前进程不同，不能用新实现重演旧观察"
    if test_epoch is not None and (
        float(test_epoch["q"]) != float(config.FDR_Q)
        or bool(test_epoch["dependent"]) is not True
        or test_epoch["method_version"] != "lond-dependent-v1"
    ):
        return "LOND q/dependent/method_version 与预约时冻结口径不同"
    current_context = _factor_observation_context(row, stage, dates)
    body = json.dumps(
        current_context, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), default=str,
    )
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != latch["context_sha256"]:
        return "固定阶段/时间窗或因子恢复上下文已变化"
    recipe_path = config.REGISTRY_DIR / "recipes" / f"{row['factor_id']}.json"
    if not recipe_path.is_file():
        return "冻结因子配方文件缺失"
    if _sha256_file(recipe_path) != row.get("sealed_recipe_sha256"):
        return "冻结因子配方哈希已变化"
    try:
        recipe = json.loads(recipe_path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return "冻结因子配方无法解析"
    if (recipe.get("research_epoch") != config.RESEARCH_EPOCH
            or recipe.get("sealed_at") != row.get("sealed_at")
            or recipe.get("sealed_data_end") != row.get("sealed_data_end")):
        return "冻结因子配方元数据已变化"
    return None


def _finalize_unreplayable_factor_observation(
    c, row: dict, latch: dict, *, stage: str, reason: str,
) -> str:
    """不读取新数据，用预约时冻结的上下文保守消费 p=1。"""
    fid = row["factor_id"]
    context = json.loads(latch["context_json"])
    frozen_epoch = reg.frozen_test_epoch(c, latch["epoch"])
    evidence = {
        "verdict": "ERROR",
        "reason": f"中断观察无法原样重演，保守按 p=1 拒绝: {reason}",
        "p_value_source": "conservative_unreplayable_observation_default",
        "new_data_used_for_decision": False,
        "recovery": {
            "reserved_at": latch.get("reserved_at"),
            "frozen_data_key": latch["data_key"],
            "producer_type": latch.get("producer_type"),
            "context_sha256": latch.get("context_sha256"),
            "failure": reason,
        },
        "oos_ic": {
            "p_value": 1.0,
            "p_value_method": latch["p_value_method"],
        },
        "window_role": stage,
        "research_epoch": config.RESEARCH_EPOCH,
        "factor_seal": {
            "sealed_at": row.get("sealed_at"),
            "sealed_data_end": row.get("sealed_data_end"),
            "recipe_sha256": row.get("sealed_recipe_sha256"),
        },
    }
    test_row = reg.record_factor_test(
        c, fid, epoch=latch["epoch"], p_value=1.0, gate_passed=False,
        p_value_method=latch["p_value_method"], data_key=latch["data_key"],
        require_observation=True, producer_type="factor_backtest",
        context=context,
        q=float(frozen_epoch["q"]),
        dependent=bool(frozen_epoch["dependent"]),
        method_version=frozen_epoch["method_version"],
    )
    c.commit()
    recipe_path = config.REGISTRY_DIR / "recipes" / f"{fid}.json"
    path, sha256 = _write_factor_evidence(
        fid, stage, recipe_path, evidence, [], test_row,
    )
    reg.attach_factor_test_evidence(
        c, int(test_row["test_id"]), evidence_path=path,
        evidence_sha256=sha256,
    )
    registry_ev = _registry_evidence(evidence, path, sha256, test_row)
    reg.set_backtest(
        c, fid, "rejected", evidence["reason"], {}, evidence=registry_ev,
        backtest_epoch=latch["epoch"], gate_passed=False,
        evidence_path=path, evidence_sha256=sha256,
    )
    c.commit()
    return "rejected"


def _write_orphan_observation_evidence(latch: dict, test_row: dict,
                                       reason: str) -> tuple[str, str]:
    """为非本 producer 的孤儿预约写通用、不可覆盖的 p=1 证据。"""
    epoch_dir = str(latch["epoch"]).replace(":", "_")
    hypothesis_hash = hashlib.sha256(
        str(latch["factor_id"]).encode("utf-8")
    ).hexdigest()[:16]
    out_dir = config.REGISTRY_DIR / "evidence" / "orphan_observations" / epoch_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{int(test_row['test_id']):06d}_{hypothesis_hash}.json"
    identity = {
        "schema_version": 1,
        "evidence_stage": "orphan_observation_conservative_finalize",
        "epoch": latch["epoch"],
        "factor_id": latch["factor_id"],
        "data_key": latch["data_key"],
        "p_value_method": latch["p_value_method"],
        "producer_type": latch.get("producer_type"),
        "context_json": latch.get("context_json"),
        "context_sha256": latch.get("context_sha256"),
        "evaluator_fingerprint": latch.get("evaluator_fingerprint"),
        "reserved_at": latch.get("reserved_at"),
        "test": {
            key: test_row.get(key) for key in (
                "test_id", "hypothesis_order", "p_value", "alpha_i",
                "gate_passed", "online_reject", "data_key", "p_value_method",
                "evaluator_fingerprint",
            )
        },
    }
    payload = {
        **identity,
        "verdict": "ERROR",
        "reason": reason,
        "p_value_source": "conservative_orphan_observation_default",
        "new_data_used_for_decision": False,
    }
    body = json.dumps(
        _json_safe(payload), ensure_ascii=False, indent=2,
        sort_keys=True, allow_nan=False,
    ) + "\n"

    def accept_existing() -> tuple[str, str]:
        try:
            existing = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("孤儿观察恢复证据损坏") from exc
        if any(existing.get(key) != value for key, value in identity.items()):
            raise RuntimeError("孤儿观察恢复证据与冻结账本不一致")
        return str(path.relative_to(config.PROJECT_ROOT)), _sha256_file(path)

    if path.is_file():
        return accept_existing()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as stream:
            stream.write(body.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(tmp, path)
        except FileExistsError:
            return accept_existing()
    finally:
        tmp.unlink(missing_ok=True)
    return str(path.relative_to(config.PROJECT_ROOT)), _sha256_file(path)


def _finalize_orphan_observation(c, latch: dict, *, reason: str) -> dict:
    """在共享研究写锁下，不读取行情地保守终结孤儿假设。"""
    frozen_epoch = reg.frozen_test_epoch(c, latch["epoch"])
    test_row = reg.record_factor_test(
        c, latch["factor_id"], epoch=latch["epoch"], p_value=1.0,
        gate_passed=False, p_value_method=latch["p_value_method"],
        data_key=latch["data_key"], require_observation=True,
        q=float(frozen_epoch["q"]),
        dependent=bool(frozen_epoch["dependent"]),
        method_version=frozen_epoch["method_version"],
    )
    path, sha256 = _write_orphan_observation_evidence(latch, test_row, reason)
    reg.attach_factor_test_evidence(
        c, int(test_row["test_id"]), evidence_path=path,
        evidence_sha256=sha256,
    )
    factor = c.execute(
        "SELECT factor_id FROM factors WHERE factor_id=? AND research_epoch=?",
        (latch["factor_id"], config.RESEARCH_EPOCH),
    ).fetchone()
    if factor is not None:
        evidence = {
            "verdict": "ERROR", "reason": reason,
            "new_data_used_for_decision": False,
            "p_value_source": "conservative_orphan_observation_default",
            "oos_ic": {"p_value": 1.0,
                       "p_value_method": latch["p_value_method"]},
            "audit_evidence": {"path": path, "sha256": sha256},
        }
        reg.set_backtest(
            c, latch["factor_id"], "rejected", reason, {}, evidence=evidence,
            backtest_epoch=latch["epoch"], gate_passed=False,
            evidence_path=path, evidence_sha256=sha256,
        )
    c.commit()
    return {
        "factor_id": latch["factor_id"],
        "producer_type": latch.get("producer_type"),
        "test_id": int(test_row["test_id"]),
        "hypothesis_order": int(test_row["hypothesis_order"]),
        "evidence_path": path,
        "evidence_sha256": sha256,
    }

def _preflight_frozen_factor_sources(
    c, rows: list[dict],
) -> None:
    """Fail before Panel I/O when queued factors lack valid frozen sources."""
    seen: set[str] = set()
    for row in rows:
        fid = str(row["factor_id"])
        if fid in seen:
            continue
        seen.add(fid)
        recipe_path = config.REGISTRY_DIR / "recipes" / f"{fid}.json"
        try:
            recipe = json.loads(recipe_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(
                f"[source_record_invalid] 冻结因子配方缺失或损坏: {fid}"
            ) from exc
        reg.verify_factor_source_for_pipeline(c, row, recipe)


def run_backtesting(batch_size: int = 200) -> dict:
    """消费当前 epoch 因子，执行历史确认；有足量新数据时再执行一次前向终测。

    2024-07~2025-12 已被旧系统反复查看，只能产生 ``historical_pass``。
    24h 不定长假设流用依赖修正 LOND，全局跨批记账，不再每个小批次重置 BH。
    """
    batch_id = "backtest-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    status.set_zone_status("backtest", "running", phase="load", batch_id=batch_id)
    summary = {"batch_id": batch_id, "zone": "backtest"}
    c = None
    mutation_lock = None
    try:
        mutation_path = config.STATE_DIR / "research_mutation.lock"
        mutation_path.parent.mkdir(parents=True, exist_ok=True)
        mutation_lock = mutation_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(mutation_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            mutation_lock.close()
            mutation_lock = None
            raise RuntimeError("其他研究写任务正在运行，因子回测未启动") from exc
        reg.migrate_schema()          # M2.1：批处理入口显式迁移（进程内幂等）
        c = reg._conn()
        historical_epoch = f"{config.RESEARCH_EPOCH}:historical-confirmation"
        forward_epoch = production.forward_test_epoch()
        historical_latch = reg.pending_factor_observation(c, historical_epoch)
        forward_latch = reg.pending_factor_observation(c, forward_epoch)
        orphan_recoveries = []

        def recover_or_route(latch, stage):
            if not latch:
                return None, None
            if latch.get("producer_type") != "factor_backtest":
                recovered = _finalize_orphan_observation(
                    c, latch,
                    reason=(
                        f"原生产者 {latch.get('producer_type')} 已释放共享写锁但"
                        "观察仍未完成；为防止 24h 队列死锁，未读取新数据并保守按 p=1 终结"
                    ),
                )
                orphan_recoveries.append(recovered)
                return None, None
            try:
                return latch, _pending_factor_row(c, latch, stage=stage)[0]
            except RuntimeError as exc:
                recovered = _finalize_orphan_observation(
                    c, latch,
                    reason=(
                        f"因子观察恢复元数据无法验证: {exc}；"
                        "未读取新数据并保守按 p=1 终结"
                    ),
                )
                orphan_recoveries.append(recovered)
                return None, None

        historical_latch, historical_pending = recover_or_route(
            historical_latch, "historical_confirmation",
        )
        forward_latch, forward_pending = recover_or_route(
            forward_latch, "forward_shadow",
        )
        summary["orphan_observations_recovered"] = orphan_recoveries
        todo = reg.registered_awaiting_backtest(c, epoch=config.RESEARCH_EPOCH)
        source_rows = todo + [
            row for row in reg.factors_by_status(c, "historical_pass")
            if row.get("research_epoch") == config.RESEARCH_EPOCH
        ]
        if historical_pending:
            source_rows.append(historical_pending)
        if forward_pending:
            source_rows.append(forward_pending)
        _preflight_frozen_factor_sources(c, source_rows)

        panel = load_thin_panel()
        windows = split_research_windows(panel.dates)
        historical_dates = windows["historical_test"]
        forward_dates = windows["forward_shadow"]
        current_data_key = research_input_key(
            panel_key=getattr(panel, "_panel_cache_key", None),
        )

        # forward 阶段崩溃的因子被 daemon 恢复为 registered，不能再当作
        # 新的历史假设。各 epoch 的 pending owner 始终置于自己队列首位。
        if forward_pending:
            todo = [r for r in todo if r["factor_id"] != forward_pending["factor_id"]]
        if historical_pending:
            todo = [historical_pending] + [
                r for r in todo if r["factor_id"] != historical_pending["factor_id"]
            ]
        todo = todo[:max(1, batch_size) if historical_pending else batch_size]
        summary["n_queued"] = len(todo)
        historical_counts = {"historical_pass": 0, "rejected": 0, "invalidated": 0}
        for i, row in enumerate(todo, start=1):
            fid = row["factor_id"]
            is_pending = bool(
                historical_latch and fid == historical_latch["factor_id"]
            )
            replay_error = (
                _pending_replay_error(
                    row, historical_latch, stage="historical_confirmation",
                    dates=historical_dates, current_data_key=current_data_key,
                    test_epoch=reg.frozen_test_epoch(
                        c, historical_latch["epoch"],
                    ),
                ) if is_pending else None
            )
            if replay_error:
                final_status = _finalize_unreplayable_factor_observation(
                    c, row, historical_latch, stage="historical_confirmation",
                    reason=replay_error,
                )
            else:
                reg.set_status(c, fid, "backtesting", "历史确认与全局在线 FDR 检验中")
                c.commit()
                final_status = _evaluate_factor_stage(
                    c, panel, row, historical_dates,
                    stage="historical_confirmation", test_epoch=historical_epoch,
                )
            historical_counts[final_status] = historical_counts.get(final_status, 0) + 1
            if i % 10 == 0 or i == len(todo):
                c.commit()
                status.set_zone_status(
                    "backtest", "running", phase="backtest", batch_id=batch_id,
                    queue_depth=len(todo) - i, detail=f"{i}/{len(todo)} 历史确认")

        # 每个假设只消费自身 seal 后才到达的数据。120 个可标记信号日还需要
        # HOLD_DAYS 个尾部交易日完成标签，因此不能用一个全局旧窗口反复终测。
        forward_counts = {"production": 0, "rejected": 0, "waiting": 0}
        forward_rows = [
            r for r in reg.factors_by_status(c, "historical_pass")
            if r.get("research_epoch") == config.RESEARCH_EPOCH
        ]
        if forward_pending:
            refreshed = c.execute(
                "SELECT * FROM factors WHERE factor_id=?",
                (forward_pending["factor_id"],),
            ).fetchone()
            forward_pending = dict(refreshed) if refreshed is not None else forward_pending
            forward_rows = [forward_pending] + [
                r for r in forward_rows if r["factor_id"] != forward_pending["factor_id"]
            ]
        # 先对全部历史通过因子做便宜的成熟度分类，再只给真正可终测的因子
        # 使用 batch 配额。否则前 N 个长期 waiting 会饿死第 N+1 个已成熟因子；
        # 也不能在尚有成熟余量时把本 data_key 标成已消费。
        selected_forward, forward_waiting, forward_remaining_eligible = (
            _select_forward_work(
                forward_rows, forward_dates, batch_size=batch_size,
                pending_factor_id=(
                    forward_pending["factor_id"] if forward_pending else None
                ),
            )
        )
        forward_counts["waiting"] = forward_waiting
        forward_eligible_count = len(selected_forward) + forward_remaining_eligible
        for row, frozen_forward in selected_forward:
            is_pending = bool(forward_latch and row["factor_id"] == forward_latch["factor_id"])
            replay_error = (
                _pending_replay_error(
                    row, forward_latch, stage="forward_shadow",
                    dates=frozen_forward, current_data_key=current_data_key,
                    test_epoch=reg.frozen_test_epoch(c, forward_latch["epoch"]),
                ) if is_pending else None
            )
            if replay_error:
                final_status = _finalize_unreplayable_factor_observation(
                    c, row, forward_latch, stage="forward_shadow",
                    reason=replay_error,
                )
                forward_counts[final_status] = forward_counts.get(final_status, 0) + 1
                continue
            fid = row["factor_id"]
            reg.set_status(c, fid, "backtesting", "seal 后冻结前向终测中")
            c.commit()
            final_status = _evaluate_factor_stage(
                c, panel, row, frozen_forward,
                stage="forward_shadow", test_epoch=forward_epoch,
            )
            forward_counts[final_status] = forward_counts.get(final_status, 0) + 1

        c.commit()
        c.close()
        c = None
        summary["research_epoch"] = config.RESEARCH_EPOCH
        summary["historical_confirmation"] = historical_counts
        summary["n_historical_pass"] = historical_counts.get("historical_pass", 0)
        summary["forward_shadow_days"] = len(forward_dates)
        summary["forward_ready"] = bool(
            forward_counts.get("production") or forward_counts.get("rejected")
        )
        summary["forward_waiting"] = forward_counts.get("waiting", 0)
        summary["forward_eligible"] = forward_eligible_count
        summary["forward_processed"] = len(selected_forward)
        summary["forward_remaining_eligible"] = forward_remaining_eligible
        summary["forward_test"] = forward_counts
        summary["n_production"] = forward_counts.get("production", 0)
        summary["n_rejected"] = (historical_counts.get("rejected", 0)
                                 + forward_counts.get("rejected", 0))
        summary["ok"] = True
        status.mark_success("backtest", batch_id,
                            detail=(f"历史确认{len(todo)}个 通过{summary['n_historical_pass']}；"
                                    f"前向全局{len(forward_dates)}日，"
                                    f"逐因子等待{summary['forward_waiting']}"),
                            success_rate=(round(summary["n_historical_pass"] / len(todo), 4)
                                          if todo else 0.0),
                            metrics={"queued": len(todo),
                                     "historical_pass": summary["n_historical_pass"],
                                     "production": summary["n_production"],
                                     "rejected": summary["n_rejected"],
                                     "forward_shadow_days": len(forward_dates),
                                     "forward_required_days": config.FORWARD_SHADOW_MIN_DAYS,
                                     "forward_waiting": summary["forward_waiting"],
                                     "forward_remaining_eligible": forward_remaining_eligible,
                                     "research_epoch": config.RESEARCH_EPOCH})
        return summary
    except Exception as e:
        summary["ok"] = False
        summary["error"] = str(e)
        status.set_zone_status("backtest", "error", phase="failed",
                               batch_id=batch_id, last_error=str(e),
                               detail="回测批异常")
        raise
    finally:
        if c is not None:
            try:
                c.close()  # 回滚未提交事务并释放 WAL 锁；backtesting 已在计算前提交
            except Exception:
                pass
        if mutation_lock is not None:
            try:
                fcntl.flock(mutation_lock.fileno(), fcntl.LOCK_UN)
            finally:
                mutation_lock.close()
        gc.collect()


def _gate_metrics(evidence: dict) -> dict:
    """从科学闸门证据提取 registry 展示列所需的 metrics。"""
    full = evidence.get("full") or {}
    return {
        "annual_return": full.get("annual_return"),
        "sharpe": full.get("sharpe"),
        "max_drawdown": full.get("max_drawdown"),
        "win_rate": full.get("win_rate"),
        "excess_annual": full.get("excess_annual"),
        "benchmark_annual": full.get("benchmark_annual"),
    }


def run_full(quick: bool = False) -> dict:
    """端到端：挖 → 验 → 入 → 回测。"""
    mine = run_mining(quick=quick)
    if mine.get("validated", 0) == 0:
        mine["note"] = "本轮无新入库因子，跳过回测"
        return {"mining": mine, "backtest": None}
    bt = run_backtesting()
    return {"mining": mine, "backtest": bt}


def _elapsed_s(summary: dict) -> float:
    try:
        import os
        start = summary.get("_t0")
        if start is None:
            import time
            summary["_t0"] = time.time()
            return 0.001
        import time
        return time.time() - start
    except Exception:
        return 1.0
