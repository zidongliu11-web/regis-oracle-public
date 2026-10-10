"""可审计的 production 因子目录与全市场分数制品。

只有当前 research epoch、已通过冻结前向 LOND、配方与证据哈希均可复核的
因子才能进入目录。投产先用原 ``rebuild_factor`` 在完整研究截面上重建，再把
内容寻址分数制品与 ``production`` 状态放进同一 SQLite 事务；读取端仍会在
完整最新 Panel 上复算，最后一步才切自选池。
"""
from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
from dataclasses import fields
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from . import registry as reg
from .data import (PANEL_UNIVERSE_SCOPE, Panel, _panel_cache_path, load_panel,
                   mask_signal_eligible)
from .gate import research_input_key
from .search import build_atom_frames, rebuild_factor


_SCORE_SCHEMA_VERSION = 4
_NORMALIZATION = "sh_sz_main_chinext_zscore_ddof0"
_EVALUATOR_SOURCE_FILES = (
    "config.py", "search.py", "templates.py", "operators.py", "preprocess.py",
    "data.py", "v4_panel.py", "source_gate.py", "validate.py", "metrics.py",
    "gate.py", "backtest.py", "pipeline.py", "registry.py", "production.py",
    "../quantqc/costs.py", "../quantqc/stats.py",
)


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_bytes(value: dict) -> bytes:
    return (json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ) + "\n").encode("utf-8")


def evaluator_fingerprint() -> str:
    """当前配方 evaluator、信号 mask 与分数 schema 的确定性身份。"""
    package_dir = Path(__file__).resolve().parent
    source_hashes = {
        name: _sha256_file(package_dir / name)
        for name in _EVALUATOR_SOURCE_FILES
    }
    manifest = {
        "score_schema_version": _SCORE_SCHEMA_VERSION,
        "normalization": _NORMALIZATION,
        "universe_scope": PANEL_UNIVERSE_SCOPE,
        "source_sha256": source_hashes,
        "signal_mask_sha256": _sha256_bytes(
            inspect.getsource(mask_signal_eligible).encode("utf-8")
        ),
        "production_scoring_sha256": _sha256_bytes(
            (inspect.getsource(_orientation) + inspect.getsource(_score_payload)).encode(
                "utf-8"
            )
        ),
        "panel_schema": [
            {"name": field.name, "type": str(field.type)} for field in fields(Panel)
        ],
        "numpy": np.__version__,
        "pandas": pd.__version__,
    }
    return _sha256_bytes(_canonical_bytes(manifest))


def _inside_project(relative_path: str) -> Path:
    root = config.PROJECT_ROOT.resolve()
    path = (root / str(relative_path)).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise RuntimeError("production 证据路径越出项目目录") from exc
    return path


def _orientation(row: dict) -> float:
    value = row.get("is_ic_mean")
    try:
        return 1.0 if float(value) >= 0 else -1.0
    except (TypeError, ValueError):
        raise RuntimeError(f"production 因子缺少冻结发现方向: {row['factor_id']}")


def _verified_recipe(c, row: dict) -> dict:
    fid = str(row["factor_id"])
    required = (
        "sealed_at", "sealed_data_end", "sealed_recipe_sha256",
        "research_record_json", "research_record_sha256",
    )
    if row.get("research_epoch") != config.RESEARCH_EPOCH:
        raise RuntimeError(f"非当前 epoch 因子不得投产: {fid}")
    if any(not row.get(key) for key in required):
        raise RuntimeError(f"production 因子 seal 不完整: {fid}")
    path = config.REGISTRY_DIR / "recipes" / f"{fid}.json"
    try:
        raw = path.read_bytes()
        recipe = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"production 因子配方缺失或损坏: {fid}") from exc
    if (_sha256_bytes(raw) != row["sealed_recipe_sha256"]
            or not isinstance(recipe, dict)
            or recipe.get("research_epoch") != config.RESEARCH_EPOCH
            or recipe.get("sealed_at") != row["sealed_at"]
            or recipe.get("sealed_data_end") != row["sealed_data_end"]
            or recipe.get("research_record_sha256")
            != row["research_record_sha256"]):
        raise RuntimeError(f"production 因子配方 seal/hash 复核失败: {fid}")
    try:
        reg.verify_factor_source_for_pipeline(c, row, recipe)
        reg.recipe_execution_hold_days(recipe)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    return recipe


def _verified_forward_evidence(c, row: dict, *, test_epoch: str,
                               evidence_path: str, evidence_sha256: str,
                               recipe: dict,
                               expected_evaluator: str | None = None) -> dict:
    try:
        return reg.verify_forward_production_semantics(
            c, row, test_epoch=test_epoch, evidence_path=evidence_path,
            evidence_sha256=evidence_sha256,
            evaluator_fingerprint=expected_evaluator,
            recipe=recipe,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _score_payload(panel, row: dict, recipe: dict, *, data_key: str,
                   atom_frames: dict | None = None) -> dict:
    """完整研究截面重建并冻结最后一日方向化 z 分数。"""
    active_evaluator = _require_process_evaluator_current()
    factor = rebuild_factor(panel, recipe, atom_frames=atom_frames)
    if not isinstance(factor, pd.DataFrame) or factor.empty:
        raise RuntimeError(f"production 因子无法用原 evaluator 重建: {row['factor_id']}")
    if not getattr(panel, "dates", None):
        raise RuntimeError("完整 Panel 无交易日")
    as_of = pd.Timestamp(max(panel.dates))
    if as_of not in factor.index:
        raise RuntimeError(f"production 因子缺 Panel 最新日分数: {row['factor_id']}")
    raw = pd.to_numeric(factor.loc[as_of], errors="coerce")
    raw = raw[np.isfinite(raw.to_numpy(dtype=float))].astype(float)
    if len(raw) < 2:
        raise RuntimeError(f"production 因子最新日全市场有效样本不足: {row['factor_id']}")
    std = float(raw.std(ddof=0))
    if not math.isfinite(std) or std <= 0:
        raise RuntimeError(f"production 因子最新日全市场分数无横截面差异: {row['factor_id']}")
    oriented = ((raw - float(raw.mean())) / std) * _orientation(row)
    scores = {
        str(code): float(value)
        for code, value in oriented.sort_index().items()
        if math.isfinite(float(value))
    }
    return {
        "schema_version": _SCORE_SCHEMA_VERSION,
        "factor_id": str(row["factor_id"]),
        "research_epoch": config.RESEARCH_EPOCH,
        "recipe_sha256": str(row["sealed_recipe_sha256"]),
        "research_record_sha256": str(row["research_record_sha256"]),
        "forward_evidence_path": str(row["evidence_path"]),
        "forward_evidence_sha256": str(row["evidence_sha256"]),
        "production_admission_path": str(row["production_admission_path"]),
        "production_admission_sha256": str(row["production_admission_sha256"]),
        "data_key": str(data_key),
        "as_of": as_of.date().isoformat(),
        "orientation": _orientation(row),
        "normalization": _NORMALIZATION,
        "universe_scope": PANEL_UNIVERSE_SCOPE,
        "evaluator_fingerprint": active_evaluator,
        "n_scores": len(scores),
        "scores": scores,
    }


_PROCESS_EVALUATOR_FINGERPRINT = evaluator_fingerprint()


def process_evaluator_fingerprint() -> str:
    """进程启动时实际加载的 evaluator 身份，供 daemon 复合触发/重启。"""
    return _PROCESS_EVALUATOR_FINGERPRINT


def forward_test_epoch(evaluator: str | None = None) -> str:
    """同一 research epoch 只有一个连续的前向 LOND 假设流。"""
    return f"{config.RESEARCH_EPOCH}:forward-shadow-sealed-v2"


def _require_process_evaluator_current() -> str:
    current = evaluator_fingerprint()
    if current != _PROCESS_EVALUATOR_FINGERPRINT:
        raise RuntimeError("production evaluator 源码已热变更，必须重启进程后重验")
    return current


def _write_content_addressed_score(payload: dict) -> dict:
    body = _canonical_bytes(payload)
    sha256 = _sha256_bytes(body)
    out_dir = config.REGISTRY_DIR / "production" / config.RESEARCH_EPOCH
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{sha256}.json"
    if path.is_file():
        if _sha256_file(path) != sha256:
            raise RuntimeError("production 内容寻址路径存在但 hash 不符")
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
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        if _sha256_file(path) != sha256:
            raise RuntimeError("production 分数制品原子落盘后 hash 不符")
    return {
        "path": str(path.relative_to(config.PROJECT_ROOT)),
        "sha256": sha256,
        "data_key": payload["data_key"],
        "as_of": payload["as_of"],
        "evaluator_fingerprint": payload["evaluator_fingerprint"],
    }


def promote_factor(c, panel, fid: str, *, metrics: dict,
                   registry_evidence: dict, test_epoch: str,
                   evidence_path: str, evidence_sha256: str,
                   reason: str) -> dict:
    """生成可消费制品，并把制品元数据和 production 状态原子提交。"""
    row = c.execute("SELECT * FROM factors WHERE factor_id=?", (fid,)).fetchone()
    if row is None:
        raise ValueError(f"factor 不存在: {fid}")
    row = dict(row)
    row["evidence_path"] = evidence_path
    row["evidence_sha256"] = evidence_sha256
    recipe = _verified_recipe(c, row)
    verified = _verified_forward_evidence(
        c, row, test_epoch=test_epoch, evidence_path=evidence_path,
        evidence_sha256=evidence_sha256,
        recipe=recipe,
    )
    admission_path = row.get("production_admission_path")
    admission_sha256 = row.get("production_admission_sha256")
    if not admission_path or not admission_sha256:
        raise RuntimeError("production promotion_pending：缺少独立 admission certificate")
    try:
        reg.verify_production_admission_certificate(
            c, row, recipe, verified, certificate_path=admission_path,
            certificate_sha256=admission_sha256,
        )
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    panel_key = getattr(panel, "_panel_cache_key", None)
    actual_data_key = research_input_key(panel_key=panel_key)
    if verified["test"].get("data_key") != actual_data_key:
        raise RuntimeError("production Panel 输入身份与冻结前向检验不一致")
    atoms = build_atom_frames(panel) if recipe.get("kind") == "genetic" else None
    payload = _score_payload(
        panel, row, recipe, data_key=actual_data_key, atom_frames=atoms,
    )
    artifact = _write_content_addressed_score(payload)

    if c.in_transaction:
        raise RuntimeError("production 原子提交前存在未结束事务")
    try:
        c.execute("BEGIN IMMEDIATE")
        locked = c.execute(
            "SELECT * FROM factors WHERE factor_id=?", (fid,),
        ).fetchone()
        if locked is None:
            raise RuntimeError(f"production 因子在提交前消失: {fid}")
        locked = dict(locked)
        locked["evidence_path"] = evidence_path
        locked["evidence_sha256"] = evidence_sha256
        locked_recipe = _verified_recipe(c, locked)
        locked_verified = _verified_forward_evidence(
            c, locked, test_epoch=test_epoch, evidence_path=evidence_path,
            evidence_sha256=evidence_sha256,
            recipe=locked_recipe,
        )
        reg.verify_production_admission_certificate(
            c, locked, locked_recipe, locked_verified,
            certificate_path=locked["production_admission_path"],
            certificate_sha256=locked["production_admission_sha256"],
        )
        reg.set_backtest(
            c, fid, "production", reason, metrics,
            evidence=registry_evidence, backtest_epoch=test_epoch,
            gate_passed=True, evidence_path=evidence_path,
            evidence_sha256=evidence_sha256,
            production_artifact=artifact,
        )
        c.commit()
    except Exception:
        c.rollback()
        raise
    return {**artifact, "factor_id": fid}


def _verified_artifact(row: dict, *, require_current_evaluator: bool = True) -> dict:
    path = _inside_project(row["production_score_path"])
    try:
        raw = path.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"production 分数制品缺失或损坏: {row['factor_id']}"
        ) from exc
    stored_evaluator = row.get("production_evaluator_sha256")
    if (not isinstance(payload, dict)
            or _sha256_bytes(raw) != row["production_score_sha256"]
            or path.name != f"{row['production_score_sha256']}.json"
            or payload.get("schema_version") != _SCORE_SCHEMA_VERSION
            or payload.get("factor_id") != row["factor_id"]
            or payload.get("research_epoch") != config.RESEARCH_EPOCH
            or payload.get("recipe_sha256") != row["sealed_recipe_sha256"]
            or payload.get("research_record_sha256")
            != row.get("research_record_sha256")
            or payload.get("forward_evidence_path") != row["evidence_path"]
            or payload.get("forward_evidence_sha256") != row["evidence_sha256"]
            or payload.get("production_admission_path")
            != row.get("production_admission_path")
            or payload.get("production_admission_sha256")
            != row.get("production_admission_sha256")
            or payload.get("data_key") != row["production_data_key"]
            or payload.get("as_of") != row["production_as_of"]
            or payload.get("normalization") != _NORMALIZATION
            or payload.get("universe_scope") != PANEL_UNIVERSE_SCOPE
            or payload.get("evaluator_fingerprint") != stored_evaluator
            or not isinstance(payload.get("scores"), dict)
            or int(payload.get("n_scores") or 0) != len(payload["scores"])
            or not payload["scores"]):
        raise RuntimeError(f"production 分数制品身份/hash 复核失败: {row['factor_id']}")
    if require_current_evaluator:
        current_evaluator = _require_process_evaluator_current()
        if stored_evaluator != current_evaluator:
            raise RuntimeError(
                f"production evaluator 已漂移，需重验后刷新: {row['factor_id']}"
            )
    return payload


def catalog_identity() -> dict:
    """轻量读取当前目录版本；目录与状态不一致时直接报错。"""
    c = reg._conn(readonly=True, migrate=False)
    try:
        catalog = reg.production_catalog(c, config.RESEARCH_EPOCH)
    finally:
        c.close()
    return catalog or {
        "research_epoch": config.RESEARCH_EPOCH,
        "catalog_version": None,
        "manifest": [],
    }


def current_input_identity() -> str:
    """当前磁盘 Panel manifest + 上证基准 hash；不加载 300MB Panel。"""
    return research_input_key(panel_key=_panel_cache_path().name)


def freeze_evaluator_drifted_production(
    panel, expected_input_key: str | None, *, dry_run: bool = False,
) -> dict | None:
    """代码版本变化时摘出旧授权；旧前向证据绝不背书新 evaluator。"""
    active_evaluator = _require_process_evaluator_current()
    identity = catalog_identity()
    stored = {
        item["production_evaluator_sha256"] for item in identity["manifest"]
    }
    if not stored or stored == {active_evaluator}:
        return None
    if len(stored) != 1:
        raise RuntimeError("production catalog 混入多个 evaluator 版本")
    panel_key = getattr(panel, "_panel_cache_key", None)
    expected_input_key = expected_input_key or (
        research_input_key(panel_key=panel_key) if panel_key else None
    )
    if (not panel_key or not expected_input_key
            or research_input_key(panel_key=panel_key) != expected_input_key
            or current_input_identity() != expected_input_key):
        raise RuntimeError("evaluator 漂移冻结缺少可信当前 Panel 身份")
    # 在动状态前完整复核旧证据/制品；损坏目录不能被静默“清理”掉。
    catalog = verified_catalog(require_current_evaluator=False)
    factor_ids = sorted(catalog.get("factors") or {})
    summary = {
        "frozen": factor_ids,
        "count": len(factor_ids),
        "old_evaluator_fingerprint": next(iter(stored)),
        "evaluator_fingerprint": active_evaluator,
        "reason": (
            "evaluator 已变化；旧冻结前向证据/LOND 不得授权新实现，"
            "需显式 research epoch 迁移后重新前向终测"
        ),
        "dry_run": bool(dry_run),
    }
    if dry_run:
        return summary
    c = reg._conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        if (current_input_identity() != expected_input_key
                or _require_process_evaluator_current() != active_evaluator):
            raise RuntimeError("evaluator 漂移冻结提交前输入/代码身份已变化")
        locked = reg.production_catalog(c, config.RESEARCH_EPOCH)
        if (locked is None
                or locked["catalog_version"] != catalog["catalog_version"]):
            raise RuntimeError("production catalog 在 evaluator 漂移冻结期间已变化")
        placeholders = ",".join("?" for _ in factor_ids)
        changed = c.execute(
            f"""UPDATE factors SET status='frozen',status_reason=?,updated_at=?
                   WHERE status='production' AND research_epoch=?
                     AND factor_id IN ({placeholders})""",
            (
                summary["reason"], reg._now(), config.RESEARCH_EPOCH,
                *factor_ids,
            ),
        ).rowcount
        if changed != len(factor_ids):
            raise RuntimeError("production 因子在 evaluator 漂移冻结期间已变化")
        refreshed = reg._refresh_production_catalog(c, config.RESEARCH_EPOCH)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    summary["catalog_version"] = refreshed["catalog_version"]
    return summary


def verified_catalog(*, require_current_evaluator: bool = True) -> dict:
    """复核当前目录全部 recipe、前向证据和 production score artifact。"""
    c = reg._conn(readonly=True, migrate=False)
    try:
        catalog = reg.production_catalog(c, config.RESEARCH_EPOCH)
        if catalog is None:
            return {
                "complete": False, "research_epoch": config.RESEARCH_EPOCH,
                "catalog_version": None, "manifest": [], "factors": {},
                "authorized_data_key": None,
                "evaluator_fingerprint": evaluator_fingerprint(),
                "universe_scope": PANEL_UNIVERSE_SCOPE,
            }
        factors = {}
        recipes = {}
        for item in catalog["manifest"]:
            raw = c.execute(
                "SELECT * FROM factors WHERE factor_id=? AND status='production' "
                "AND research_epoch=?",
                (item["factor_id"], config.RESEARCH_EPOCH),
            ).fetchone()
            if raw is None:
                raise RuntimeError("production catalog 含非生产或旧 epoch 因子")
            row = dict(raw)
            recipe = _verified_recipe(c, row)
            recipes[row["factor_id"]] = recipe
            verified = _verified_forward_evidence(
                c, row, test_epoch=row["backtest_epoch"],
                evidence_path=row["evidence_path"],
                evidence_sha256=row["evidence_sha256"],
                recipe=recipe,
                expected_evaluator=row["production_evaluator_sha256"],
            )
            try:
                reg.verify_production_admission_certificate(
                    c, row, recipe, verified,
                    certificate_path=row["production_admission_path"],
                    certificate_sha256=row["production_admission_sha256"],
                )
            except ValueError as exc:
                raise RuntimeError(str(exc)) from exc
            _verified_artifact(
                row, require_current_evaluator=require_current_evaluator,
            )
            factors[row["factor_id"]] = {
                "factor_id": row["factor_id"], "name": row["name"],
                "family": row["family"], "status": row["status"],
                "research_epoch": row["research_epoch"],
                "orientation_multiplier": _orientation(row),
                "recipe_sha256": row["sealed_recipe_sha256"],
                "research_record_sha256": row["research_record_sha256"],
                "forward_evidence": {
                    "test_epoch": row["backtest_epoch"],
                    "path": row["evidence_path"],
                    "sha256": row["evidence_sha256"],
                },
                "production_admission": {
                    "path": row["production_admission_path"],
                    "sha256": row["production_admission_sha256"],
                },
                "score_artifact": {
                    "path": row["production_score_path"],
                    "sha256": row["production_score_sha256"],
                    "data_key": row["production_data_key"],
                    "as_of": row["production_as_of"],
                    "evaluator_fingerprint": row["production_evaluator_sha256"],
                    "universe_scope": PANEL_UNIVERSE_SCOPE,
                },
            }
        authorized_keys = {
            item["score_artifact"]["data_key"] for item in factors.values()
        }
        evaluator_keys = {
            item["score_artifact"]["evaluator_fingerprint"]
            for item in factors.values()
        }
        if len(authorized_keys) > 1:
            raise RuntimeError("production catalog 混入多个授权输入版本")
        if len(evaluator_keys) > 1:
            raise RuntimeError("production catalog 混入多个 evaluator 版本")
        return {
            **catalog,
            "complete": bool(factors),
            "factors": factors,
            "recipes": recipes,
            "source": "factor_system/registry/registry.db:production_catalog",
            "universe_scope": PANEL_UNIVERSE_SCOPE,
            "required_factor_ids": sorted(factors),
            "authorized_data_key": next(iter(authorized_keys), None),
            "evaluator_fingerprint": next(
                iter(evaluator_keys), evaluator_fingerprint(),
            ),
        }
    finally:
        c.close()


def load_verified_scores(watchlist: list[str], panel=None) -> tuple[dict, dict]:
    """当前 production 在最新研究截面复算后，最后一步切 watchlist。"""
    catalog = verified_catalog()
    if not catalog.get("complete"):
        return {}, catalog
    panel = panel if panel is not None else load_panel()
    panel_key = getattr(panel, "_panel_cache_key", None)
    if not panel_key:
        raise RuntimeError("production Panel 缺少实际输入身份")
    data_key = research_input_key(panel_key=panel_key)
    if data_key != catalog.get("authorized_data_key"):
        raise RuntimeError(
            "production 当前输入尚未完成重验授权: "
            f"{catalog.get('authorized_data_key')} -> {data_key}"
        )
    recipes = catalog.pop("recipes")
    need_atoms = any(recipe.get("kind") == "genetic" for recipe in recipes.values())
    atoms = build_atom_frames(panel) if need_atoms else None
    per_factor = {}
    as_ofs = set()
    for fid in sorted(recipes):
        row = {
            "factor_id": fid,
            "research_epoch": config.RESEARCH_EPOCH,
            "sealed_recipe_sha256": catalog["factors"][fid]["recipe_sha256"],
            "research_record_sha256": catalog["factors"][fid][
                "research_record_sha256"
            ],
            "evidence_path": catalog["factors"][fid]["forward_evidence"]["path"],
            "evidence_sha256": catalog["factors"][fid]["forward_evidence"]["sha256"],
            "production_admission_path": catalog["factors"][fid][
                "production_admission"
            ]["path"],
            "production_admission_sha256": catalog["factors"][fid][
                "production_admission"
            ]["sha256"],
            "is_ic_mean": catalog["factors"][fid]["orientation_multiplier"],
        }
        payload = _score_payload(
            panel, row, recipes[fid], data_key=data_key, atom_frames=atoms,
        )
        per_factor[fid] = payload["scores"]
        as_ofs.add(payload["as_of"])
    if len(as_ofs) != 1:
        raise RuntimeError("production 因子运行时 as_of 不一致")
    common_codes = set.intersection(*(set(scores) for scores in per_factor.values()))
    combined = {
        code: float(np.mean([per_factor[fid][code] for fid in sorted(per_factor)]))
        for code in common_codes
    }
    by_six = {str(code)[:6]: value for code, value in combined.items()}
    version = catalog["catalog_version"]
    as_of = next(iter(as_ofs))
    values = {
        code: {
            "_production_score": by_six[code[:6]],
            "_as_of": as_of,
            "_catalog_version": version,
            "_production_data_key": data_key,
            "_production_factor_count": len(per_factor),
        }
        for code in watchlist if code[:6] in by_six
    }
    catalog.update({
        "runtime_data_key": data_key,
        "runtime_as_of": as_of,
        "runtime_universe_scope": PANEL_UNIVERSE_SCOPE,
        "runtime_research_universe_coverage": len(combined),
        "runtime_full_market_coverage": len(combined),
        "runtime_watchlist_coverage": len(values),
    })
    if current_input_identity() != data_key:
        raise RuntimeError("production 评分期间当前输入身份已变化")
    if _require_process_evaluator_current() != catalog["evaluator_fingerprint"]:
        raise RuntimeError("production 评分期间 evaluator 身份已变化")
    if catalog_identity().get("catalog_version") != version:
        raise RuntimeError("production 评分期间 catalog 版本已变化")
    return values, catalog


def refresh_authorized_catalog(panel, expected_input_key: str) -> dict:
    """重验整轮成功后，原子刷新全部 production 制品与授权 data_key。"""
    active_evaluator = _require_process_evaluator_current()
    panel_key = getattr(panel, "_panel_cache_key", None)
    if not panel_key or not expected_input_key:
        raise RuntimeError("production 授权刷新缺少 Panel/expected input identity")
    actual_input_key = research_input_key(panel_key=panel_key)
    if (actual_input_key != expected_input_key
            or current_input_identity() != expected_input_key):
        raise RuntimeError(
            "production 授权刷新输入已变化: "
            f"{expected_input_key} -> {actual_input_key}"
        )
    drift = freeze_evaluator_drifted_production(panel, expected_input_key)
    if drift:
        raise RuntimeError(
            "production evaluator 已漂移；旧授权因子已冻结，"
            "禁止用旧前向证据直接 refresh"
        )

    catalog = verified_catalog(require_current_evaluator=False)
    factor_ids = sorted(catalog.get("factors") or {})
    if not factor_ids:
        return {
            "ok": True,
            "research_epoch": config.RESEARCH_EPOCH,
            "authorized_data_key": None,
            "catalog_version": catalog.get("catalog_version"),
            "evaluator_fingerprint": active_evaluator,
            "factor_ids": [],
            "refreshed_count": 0,
            "as_of": None,
        }

    recipes = catalog["recipes"]
    atoms = (
        build_atom_frames(panel)
        if any(recipe.get("kind") == "genetic" for recipe in recipes.values())
        else None
    )
    artifacts = {}
    for fid in factor_ids:
        metadata = catalog["factors"][fid]
        row = {
            "factor_id": fid,
            "sealed_recipe_sha256": metadata["recipe_sha256"],
            "research_record_sha256": metadata["research_record_sha256"],
            "evidence_path": metadata["forward_evidence"]["path"],
            "evidence_sha256": metadata["forward_evidence"]["sha256"],
            "production_admission_path": metadata["production_admission"]["path"],
            "production_admission_sha256": metadata["production_admission"]["sha256"],
            "is_ic_mean": metadata["orientation_multiplier"],
        }
        payload = _score_payload(
            panel, row, recipes[fid], data_key=actual_input_key,
            atom_frames=atoms,
        )
        artifacts[fid] = _write_content_addressed_score(payload)
    as_ofs = {artifact["as_of"] for artifact in artifacts.values()}
    if len(as_ofs) != 1:
        raise RuntimeError("production 授权刷新 as_of 不一致")

    c = reg._conn()
    try:
        c.execute("BEGIN IMMEDIATE")
        if (current_input_identity() != expected_input_key
                or _require_process_evaluator_current() != active_evaluator):
            raise RuntimeError("production 授权刷新提交前输入/evaluator 已变化")
        locked_catalog = reg.production_catalog(c, config.RESEARCH_EPOCH)
        if (locked_catalog is None
                or locked_catalog["catalog_version"] != catalog["catalog_version"]):
            raise RuntimeError("production catalog 在授权刷新期间已变化")
        locked_ids = sorted(item["factor_id"] for item in locked_catalog["manifest"])
        if locked_ids != factor_ids:
            raise RuntimeError("production 因子集合在授权刷新期间已变化")
        for fid in factor_ids:
            raw = c.execute(
                "SELECT * FROM factors WHERE factor_id=? AND status='production' "
                "AND research_epoch=?",
                (fid, config.RESEARCH_EPOCH),
            ).fetchone()
            if raw is None:
                raise RuntimeError(f"production 因子在授权刷新期间失效: {fid}")
            row = dict(raw)
            recipe = _verified_recipe(c, row)
            verified = _verified_forward_evidence(
                c, row, test_epoch=row["backtest_epoch"],
                evidence_path=row["evidence_path"],
                evidence_sha256=row["evidence_sha256"],
                recipe=recipe,
            )
            reg.verify_production_admission_certificate(
                c, row, recipe, verified,
                certificate_path=row["production_admission_path"],
                certificate_sha256=row["production_admission_sha256"],
            )
            artifact = artifacts[fid]
            candidate = {
                **row,
                "production_score_path": artifact["path"],
                "production_score_sha256": artifact["sha256"],
                "production_data_key": artifact["data_key"],
                "production_as_of": artifact["as_of"],
                "production_evaluator_sha256": artifact["evaluator_fingerprint"],
            }
            _verified_artifact(candidate)
            c.execute(
                """UPDATE factors SET production_score_path=?,
                           production_score_sha256=?,production_data_key=?,
                           production_as_of=?,production_evaluator_sha256=?,updated_at=?
                     WHERE factor_id=? AND status='production' AND research_epoch=?""",
                (
                    artifact["path"], artifact["sha256"], artifact["data_key"],
                    artifact["as_of"], artifact["evaluator_fingerprint"],
                    reg._now(), fid, config.RESEARCH_EPOCH,
                ),
            )
        refreshed = reg._refresh_production_catalog(c, config.RESEARCH_EPOCH)
        c.commit()
    except Exception:
        c.rollback()
        raise
    finally:
        c.close()
    return {
        "ok": True,
        "research_epoch": config.RESEARCH_EPOCH,
        "authorized_data_key": actual_input_key,
        "catalog_version": refreshed["catalog_version"],
        "evaluator_fingerprint": active_evaluator,
        "factor_ids": factor_ids,
        "refreshed_count": len(factor_ids),
        "as_of": next(iter(as_ofs)),
    }
