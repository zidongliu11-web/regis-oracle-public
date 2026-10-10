"""Fail-closed source and preregistration gate for factor candidates."""

from __future__ import annotations

import csv
from copy import deepcopy
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import stat as stat_module
from typing import Any, Iterable, Mapping

from . import config


PROJECT_ROOT = config.PROJECT_ROOT
CATALOG_PATH = config.SOURCE_CATALOG
_HEX40 = re.compile(r"^[0-9a-f]{40}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_VERSIONED_CURRENT_RESULT = re.compile(
    r"^current_v[0-9]+_return_result_read$"
)
_PREBUILD_AUTHORIZATION_GATE_COMMAND = (
    "PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile "
    "factor_system/source_gate.py factor_system/tests/test_source_gate.py"
)
_BLIND_GUARD_PROTOCOL_PATH = (
    "research/FACTOR_BLIND_EVALUATION_GUARD_PREREGISTRATION.json"
)
_BLIND_GUARD_PROTOCOL_SHA256 = (
    "cad03d112d03358ab6687288fe236234fd286e7389849e1f62b9df0e1cb417c9"
)
_PREBUILD_PANEL_READER_SHA256 = (
    "15ff32c2b83424eb861f0e9f6530e810a3cb9347bd63d4a62446e5799b977c84"
)
_SEALED_IMPLEMENTATION_STATUS = "implementation_built"
_BLIND_REJECTED_STATUS = "blind_rejected"
_SEALED_IMPLEMENTATION_STATE = {
    "candidate_status": _SEALED_IMPLEMENTATION_STATUS,
    "implementation_authorized": True,
    "implementation_built": True,
    "implementation_admitted": True,
    "blind_evaluation_admitted": False,
    "runnable": False,
    "backtest_admitted": False,
    "production_admitted": False,
}
_SEALED_EXECUTION_PERMISSIONS = {
    "future_data_allowed": False,
    "generic_panel_allowed": False,
    "real_panel_allowed": False,
    "mining_allowed": False,
    "backtest_allowed": False,
    "production_allowed": False,
}
_BLIND_REJECTION_STATE = {
    "candidate_status": _BLIND_REJECTED_STATUS,
    "implementation_built": True,
    "implementation_admitted": True,
    "blind_evaluation_completed": True,
    "blind_evaluation_passed": False,
    "runnable": False,
    "backtest_admitted": False,
    "production_admitted": False,
}
_SEALED_AUDIT_FALSE_FIELDS = (
    "result_data_read",
    "candidate_signal_computed",
    "candidate_rank_computed",
    "candidate_outcome_computed",
    "price_delay_computed",
    "ic_or_gate_computed",
    "mining_or_backtest_run",
    "paid_api_used",
)
_BLIND_GUARD_IMPLEMENTATION_PATHS = [
    "factor_system/source_gate.py",
    "factor_system/registry.py",
    "factor_system/pipeline.py",
    "factor_system/data.py",
]
_BLIND_GUARD_TEST_PATHS = [
    "factor_system/tests/test_source_gate.py",
    "factor_system/tests/test_blind_candidate_once.py",
    "factor_system/tests/test_blind_run_registry.py",
]
_BLIND_GUARD_AUDIT_FALSE_FIELDS = (
    "real_panel_read",
    "candidate_signal_computed",
    "candidate_return_computed",
    "candidate_outcome_computed",
    "ic_or_gate_computed",
    "mining_or_backtest_run",
    "paid_api_used",
    "production_allowed",
    "automatic_trading_allowed",
)
_BLIND_EVALUATOR_PATHS = [
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
]
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
_BLIND_AUTHORIZATION_PERMISSIONS = {
    "blind_discovery_validation_once_allowed": True,
    "generic_mining_allowed": False,
    "historical_backtest_allowed": False,
    "forward_observation_allowed": False,
    "production_allowed": False,
    "paid_api_allowed": False,
    "automatic_trading_allowed": False,
}
_BLIND_AUTHORIZATION_FALSE_FIELDS = (
    "result_data_read",
    "real_panel_read",
    "candidate_signal_computed",
    "candidate_return_computed",
    "candidate_outcome_computed",
    "ic_or_gate_computed",
    "mining_or_backtest_run",
    "paid_api_used",
    "production_allowed",
    "automatic_trading_allowed",
)
_PRIOR_TRIAL_COLLECTIONS = (
    "contaminated_trials",
    "superseded_non_outcome_trials",
)
_PRIOR_TRIAL_IDENTITY_FIELDS = (
    "candidate_id",
    "status",
    "result_data_read",
    "research_record_sha256",
    "protocol_sha256",
    "admission_sha256",
)
_REQUIRED_CANDIDATE_FIELDS = {
    "candidate_id",
    "name",
    "evidence_basis",
    "source_ids",
    "mechanism",
    "formula",
    "direction",
    "data",
    "point_in_time",
    "counter_evidence",
    "license",
    "preregistration",
    "status",
    "runnable",
    "paid_api_required",
    "result_data_read",
}


class SourceCatalogError(ValueError):
    """The catalog cannot be parsed into the expected top-level object."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and bool(path.parts)
        and "." not in path.parts
        and ".." not in path.parts
        and path.as_posix() == value
    )


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _verified_local_file(
    root: Path,
    relative_path: object,
    expected_sha256: object,
    label: str,
    errors: list[str],
    *,
    keep_bytes: bool,
) -> tuple[Path | None, bytes | None]:
    """Hash one stable regular-file descriptor and optionally retain its bytes."""
    if not _safe_relative_path(relative_path):
        errors.append(f"{label}_path_invalid")
        return None, None
    if not isinstance(expected_sha256, str) or not _HEX64.fullmatch(
        expected_sha256
    ):
        errors.append(f"{label}_sha256_invalid")
        return None, None
    try:
        resolved_root = root.resolve(strict=True)
    except (OSError, RuntimeError):
        errors.append(f"{label}_missing")
        return None, None
    parts = PurePosixPath(str(relative_path)).parts
    unresolved = resolved_root.joinpath(*parts)

    def path_stat() -> os.stat_result | None:
        current = resolved_root
        try:
            for part in parts:
                current = current / part
                current_stat = os.lstat(current)
                if stat_module.S_ISLNK(current_stat.st_mode):
                    errors.append(f"{label}_symlink_forbidden")
                    return None
        except OSError:
            errors.append(f"{label}_missing")
            return None
        if not stat_module.S_ISREG(current_stat.st_mode):
            errors.append(f"{label}_not_regular_file")
            return None
        return current_stat

    initial_path_stat = path_stat()
    if initial_path_stat is None:
        return None, None
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(unresolved, flags)
    except OSError:
        errors.append(f"{label}_open_failed")
        return None, None
    try:
        before = os.fstat(descriptor)
        if (
            not stat_module.S_ISREG(before.st_mode)
            or (before.st_dev, before.st_ino)
            != (initial_path_stat.st_dev, initial_path_stat.st_ino)
        ):
            errors.append(f"{label}_identity_changed")
            return None, None
        digest = hashlib.sha256()
        chunks: list[bytes] | None = [] if keep_bytes else None
        while block := os.read(descriptor, 1024 * 1024):
            digest.update(block)
            if chunks is not None:
                chunks.append(block)
        after = os.fstat(descriptor)
        if _file_identity(after) != _file_identity(before):
            errors.append(f"{label}_identity_changed")
            return None, None
        final_path_stat = path_stat()
        if final_path_stat is None:
            return None, None
        if _file_identity(final_path_stat) != _file_identity(before):
            errors.append(f"{label}_identity_changed")
            return None, None
        if digest.hexdigest() != expected_sha256:
            errors.append(f"{label}_sha256_mismatch")
            return None, None
        return unresolved, b"".join(chunks) if chunks is not None else None
    except OSError:
        errors.append(f"{label}_read_failed")
        return None, None
    finally:
        os.close(descriptor)


def _local_file(
    root: Path,
    relative_path: object,
    expected_sha256: object,
    label: str,
    errors: list[str],
) -> Path | None:
    path, _ = _verified_local_file(
        root, relative_path, expected_sha256, label, errors,
        keep_bytes=False,
    )
    return path


def _protocol_amendment_number(candidate_id: str, amendment_id: object) -> int | None:
    if not isinstance(amendment_id, str):
        return None
    match = re.fullmatch(
        rf"{re.escape(candidate_id)}_protocol_amendment_([1-9][0-9]*)",
        amendment_id,
    )
    return int(match.group(1)) if match else None


def _prebuild_target_contract(candidate_id: str, protocol: Mapping[str, Any]) -> dict:
    """Return the immutable semantics that an implementation may realize.

    PriceDelay's older protocol uses ``frozen_method.local_identity``.  Newer
    factor protocols freeze their formula and evaluation semantics directly.
    Keeping both shapes here lets the shared gate evolve without rewriting an
    archived protocol.
    """
    frozen_local = (protocol.get("frozen_method") or {}).get("local_identity")
    if isinstance(frozen_local, Mapping) and frozen_local:
        return {
            "candidate_id": candidate_id,
            "family": frozen_local.get("family"),
            "formula": frozen_local.get("formula"),
            "parameters": frozen_local.get("params"),
            "direction": frozen_local.get("direction"),
        }
    fields = [
        "formula",
        "formation_and_execution",
        "point_in_time",
        "cost_and_capacity",
        "frozen_failure_rules",
    ]
    semantics = {field: protocol.get(field) for field in fields}
    return {
        "candidate_id": candidate_id,
        "semantics_fields": fields,
        "semantics_sha256": _sha256_json(semantics),
    }


def _verify_prebuild_recipe_contract(
    candidate_id: str,
    protocol: Mapping[str, Any],
    implementation_spec: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> None:
    """Bind a source candidate's future code shape without naming a factor."""
    local = implementation_spec.get("expected_local_implementation")
    recipe = implementation_spec.get("expected_recipe")
    required_local = {
        "factor_id", "family", "formula", "params", "data_requires", "direction",
    }
    if not isinstance(local, Mapping) or set(local) != required_local:
        errors.append(f"{prefix}.expected_local_implementation_invalid")
        return
    if (
        local.get("factor_id") != candidate_id
        or not isinstance(local.get("family"), str)
        or not local.get("family")
        or not isinstance(local.get("formula"), str)
        or not local.get("formula")
        or not isinstance(local.get("params"), Mapping)
        or not isinstance(local.get("data_requires"), list)
        or not local.get("data_requires")
        or any(not isinstance(value, str) or not value for value in local["data_requires"])
        or local.get("direction") not in {"lower_is_better", "higher_is_better"}
    ):
        errors.append(f"{prefix}.expected_local_implementation_invalid")
    if not isinstance(recipe, Mapping) or set(recipe) != {
        "kind", "id", "params", "execution_spec", "validation_spec",
    }:
        errors.append(f"{prefix}.expected_recipe_invalid")
        return
    holding_days = (protocol.get("formation_and_execution") or {}).get(
        "holding_days"
    )
    orientation = (protocol.get("formula") or {}).get("orientation")
    expected_direction = -1.0 if orientation == -1 else 1.0
    if (
        recipe.get("kind") != "source"
        or recipe.get("id") != candidate_id
        or recipe.get("params") != local.get("params")
        or recipe.get("execution_spec") != {"hold_days": holding_days}
        or recipe.get("validation_spec") != {
            "expected_direction": expected_direction,
            "apply_neutralization": False,
        }
    ):
        errors.append(f"{prefix}.expected_recipe_mismatch")


def _verify_generic_input_bindings(
    root: Path,
    bindings: object,
    feasibility: Mapping[str, Any],
    label: str,
    errors: list[str],
) -> None:
    """Bind a prebuild authorization to its candidate's feasibility inputs."""
    expected = feasibility.get("input_bindings")
    if not isinstance(expected, Mapping) or not expected:
        errors.append(f"{label}_feasibility_bindings_missing")
        return
    if bindings != expected:
        errors.append(f"{label}_mismatch")
        return
    for name, binding in expected.items():
        item_label = f"{label}.{name}"
        if not isinstance(binding, Mapping):
            errors.append(f"{item_label}_not_object")
            continue
        if name != "panel_manifest":
            _local_file(
                root, binding.get("path"), binding.get("sha256"),
                item_label, errors,
            )
            continue
        manifest = _local_artifact(
            root, binding.get("path"), binding.get("sha256"),
            item_label, errors,
        )
        if not manifest:
            continue
        recorded_panel = binding.get("recorded_panel_sha256")
        if (
            not isinstance(manifest, dict)
            or (manifest.get("outputs") or {}).get("universe_daily.parquet")
            != recorded_panel
        ):
            errors.append(f"{item_label}_recorded_panel_sha_mismatch")


def _verified_official_sources(
    root: Path,
    alias_bytes: bytes | None,
    label: str,
    errors: list[str],
) -> list[dict[str, str]]:
    if alias_bytes is None:
        return []
    try:
        reader = csv.DictReader(
            alias_bytes.decode("utf-8-sig").splitlines(),
        )
        required = {"official_source_path", "official_source_sha256"}
        if not required.issubset(set(reader.fieldnames or [])):
            errors.append(f"{label}_columns_missing")
            return []
        rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error):
        errors.append(f"{label}_csv_invalid")
        return []
    if not rows:
        errors.append(f"{label}_rows_missing")
        return []

    distinct: dict[str, str] = {}
    physical_sources: dict[tuple[int, int], str] = {}
    for index, row in enumerate(rows):
        relative_path = str(row.get("official_source_path") or "").strip()
        expected_sha256 = str(row.get("official_source_sha256") or "").strip()
        prior_sha = distinct.get(relative_path)
        if prior_sha is not None:
            if prior_sha != expected_sha256:
                errors.append(f"{label}_conflicting_sha256")
            continue
        source_path = _local_file(
            root,
            relative_path,
            expected_sha256,
            f"{label}[{index}]",
            errors,
        )
        if source_path is not None:
            source_stat = source_path.stat()
            physical_identity = (source_stat.st_dev, source_stat.st_ino)
            prior_path = physical_sources.get(physical_identity)
            if prior_path is not None and prior_path != relative_path:
                errors.append(f"{label}_duplicate_physical_source")
                continue
            physical_sources[physical_identity] = relative_path
            distinct[relative_path] = expected_sha256
    return [
        {"path": path, "sha256": distinct[path]}
        for path in sorted(distinct)
    ]


def _local_artifact(
    root: Path,
    relative_path: object,
    expected_sha256: object,
    label: str,
    errors: list[str],
) -> dict[str, Any]:
    _path, body = _verified_local_file(
        root, relative_path, expected_sha256, label, errors,
        keep_bytes=True,
    )
    if body is None:
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        errors.append(f"{label}_json_invalid")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label}_json_not_object")
        return {}
    return value


def _implementation_files(
    implementation: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Normalize legacy one-file and v3 multi-file implementation records."""
    files = implementation.get("files")
    if isinstance(files, list):
        return [dict(item) for item in files if isinstance(item, Mapping)]
    return [{
        "path": implementation.get("path"),
        "sha256": implementation.get("sha256"),
        "scope": implementation.get("scope"),
    }]


def _verify_implementation_files(
    root: Path,
    implementation: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    raw_files = implementation.get("files")
    files = _implementation_files(implementation)
    if isinstance(raw_files, list) and (
        not raw_files or len(files) != len(raw_files)
    ):
        errors.append(f"{prefix}.implementation.files_invalid")
        return []
    paths: list[str] = []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(files):
        label = (
            f"{prefix}.implementation.files[{index}]"
            if isinstance(raw_files, list) else f"{prefix}.implementation"
        )
        _local_file(
            root, item.get("path"), item.get("sha256"), label, errors,
        )
        scope = item.get("scope")
        if (
            not isinstance(scope, list)
            or not scope
            or any(not isinstance(value, str) or not value.strip() for value in scope)
        ):
            errors.append(f"{label}.scope_invalid")
        path = item.get("path")
        if isinstance(path, str):
            paths.append(path)
        normalized.append({
            "path": path,
            "sha256": item.get("sha256"),
            "scope": scope,
        })
    if len(paths) != len(set(paths)):
        errors.append(f"{prefix}.implementation.files_duplicate_path")
    return normalized


def _implementation_audit_identity(
    implementation: Mapping[str, Any],
) -> dict[str, Any]:
    files = _implementation_files(implementation)
    if isinstance(implementation.get("files"), list):
        return {"files": [
            {"path": item.get("path"), "sha256": item.get("sha256")}
            for item in files
        ]}
    item = files[0]
    return {"path": item.get("path"), "sha256": item.get("sha256")}


def _implementation_protocol_identity(
    implementation: Mapping[str, Any],
) -> dict[str, Any]:
    files = _implementation_files(implementation)
    if isinstance(implementation.get("files"), list):
        return {"files": [
            {"path": item.get("path"), "scope": item.get("scope")}
            for item in files
        ]}
    item = files[0]
    return {
        "path": item.get("path"),
        "sha256": item.get("sha256"),
        "scope": item.get("scope"),
    }


def _ordered_physical_identity(
    root: Path,
    value: object,
    expected_paths: object,
    label: str,
    errors: list[str],
) -> list[dict[str, Any]]:
    """Validate an ordered, exact path/SHA list and return its projection."""
    expected = list(expected_paths) if isinstance(expected_paths, list) else []
    if (
        not isinstance(value, list)
        or len(value) != len(expected)
        or any(not isinstance(item, Mapping) for item in value)
    ):
        errors.append(f"{label}_invalid")
        return []
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if set(item) != {"path", "sha256"}:
            errors.append(f"{label}[{index}]_fields_mismatch")
        path = item.get("path")
        sha256 = item.get("sha256")
        _local_file(root, path, sha256, f"{label}[{index}]", errors)
        normalized.append({"path": path, "sha256": sha256})
    if [item["path"] for item in normalized] != expected:
        errors.append(f"{label}_paths_mismatch")
    if len({str(item["path"]) for item in normalized}) != len(normalized):
        errors.append(f"{label}_duplicate_path")
    return normalized


def _verify_blind_guard_identity(
    root: Path,
    binding: object,
    label: str,
    errors: list[str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify the separately audited code that enforces one blind run."""
    if not isinstance(binding, Mapping):
        errors.append(f"{label}_not_object")
        return {}, {}
    if set(binding) != {"protocol", "implementation_audit"}:
        errors.append(f"{label}_fields_mismatch")
    protocol_binding = binding.get("protocol")
    if not isinstance(protocol_binding, Mapping):
        protocol_binding = {}
    expected_protocol = dict(protocol_binding)
    if set(expected_protocol) != {"path", "sha256"}:
        errors.append(f"{label}.protocol_fields_mismatch")
    protocol = _local_artifact(
        root,
        protocol_binding.get("path"),
        protocol_binding.get("sha256"),
        f"{label}.protocol",
        errors,
    )
    authorized_scope = protocol.get("authorized_implementation_scope") or {}
    expected_scope = (
        _BLIND_GUARD_IMPLEMENTATION_PATHS + _BLIND_GUARD_TEST_PATHS
    )
    if (
        protocol.get("protocol_id") != "factor_blind_evaluation_guard_v1"
        or protocol.get("status")
        != "frozen_before_guard_implementation_and_any_real_panel_read"
        or authorized_scope.get("files") != expected_scope
    ):
        errors.append(f"{label}.protocol_contract_invalid")

    audit_binding = binding.get("implementation_audit")
    if not isinstance(audit_binding, Mapping):
        errors.append(f"{label}.implementation_audit_binding_invalid")
        audit_binding = {}
    audit = _local_artifact(
        root,
        audit_binding.get("path"),
        audit_binding.get("sha256"),
        f"{label}.implementation_audit",
        errors,
    )
    if not audit:
        return protocol, {}
    if audit.get("schema_version") != 1:
        errors.append(f"{label}.implementation_audit.schema_version_invalid")
    if audit.get("status") != "passed":
        errors.append(f"{label}.implementation_audit.status_not_passed")
    if audit.get("guard_protocol") != expected_protocol:
        errors.append(f"{label}.implementation_audit.protocol_mismatch")
    for field in _BLIND_GUARD_AUDIT_FALSE_FIELDS:
        if audit.get(field) is not False:
            errors.append(
                f"{label}.implementation_audit.{field}_must_be_false"
            )
    implementation = audit.get("implementation") or {}
    _ordered_physical_identity(
        root,
        implementation.get("files"),
        _BLIND_GUARD_IMPLEMENTATION_PATHS,
        f"{label}.implementation_audit.implementation_files",
        errors,
    )
    tests = audit.get("tests") or {}
    _ordered_physical_identity(
        root,
        tests.get("paths"),
        _BLIND_GUARD_TEST_PATHS,
        f"{label}.implementation_audit.test_files",
        errors,
    )
    command = tests.get("command")
    passed = tests.get("passed")
    failed = tests.get("failed")
    if not isinstance(command, str) or not command.strip():
        errors.append(f"{label}.implementation_audit.test_command_invalid")
    if isinstance(passed, bool) or not isinstance(passed, int) or passed <= 0:
        errors.append(f"{label}.implementation_audit.test_passed_invalid")
    if isinstance(failed, bool) or failed != 0:
        errors.append(f"{label}.implementation_audit.test_failed_invalid")
    return protocol, audit


def _verify_sealed_implementation(
    root: Path,
    candidate: Mapping[str, Any],
    protocol: Mapping[str, Any],
    amendment: Mapping[str, Any],
    admission: Mapping[str, Any],
    prior_amendment: Mapping[str, Any],
    prior_admission: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> None:
    """Verify a synthetic-only build seal without granting any run capability."""
    guard_binding = amendment.get("implementation_guard") or {}
    guard_protocol_binding = guard_binding.get("protocol") or {}
    guard = _local_artifact(
        root,
        guard_protocol_binding.get("path"),
        guard_protocol_binding.get("sha256"),
        f"{prefix}.implementation_guard.protocol",
        errors,
    )
    authorized = guard.get("authorized_candidate") if guard else {}
    if (
        guard.get("protocol_id") != "factor_blind_evaluation_guard_v1"
        or guard.get("status")
        != "frozen_before_guard_implementation_and_any_real_panel_read"
        or not isinstance(authorized, Mapping)
        or authorized.get("candidate_id") != candidate.get("candidate_id")
    ):
        errors.append(f"{prefix}.implementation_guard.protocol_invalid")
        authorized = {}

    prebuild_anchor = {
        "amendment_path": authorized.get("prebuild_amendment_path"),
        "amendment_sha256": authorized.get("prebuild_amendment_sha256"),
        "admission_path": authorized.get("prebuild_admission_path"),
        "admission_sha256": authorized.get("prebuild_admission_sha256"),
    }
    if amendment.get("prebuild_authorization") != prebuild_anchor:
        errors.append(
            f"{prefix}.protocol_amendment.prebuild_authorization_mismatch"
        )
    prebuild_amendment = _local_artifact(
        root,
        prebuild_anchor["amendment_path"],
        prebuild_anchor["amendment_sha256"],
        f"{prefix}.prebuild_amendment",
        errors,
    )
    prebuild_admission = _local_artifact(
        root,
        prebuild_anchor["admission_path"],
        prebuild_anchor["admission_sha256"],
        f"{prefix}.prebuild_admission",
        errors,
    )
    prior_amendment_binding = amendment.get("prior_amendment") or {}
    prior_admission_binding = amendment.get("prior_admission") or {}
    prior_is_prebuild = (
        prior_amendment_binding.get("path") == prebuild_anchor["amendment_path"]
        and prior_amendment_binding.get("sha256") == prebuild_anchor[
            "amendment_sha256"
        ]
    )
    prior_is_sealed_successor = (
        prior_amendment.get("prebuild_authorization") == prebuild_anchor
        and prior_admission.get("prebuild_authorization") == prebuild_anchor
        and prior_admission.get("implementation_built") is True
        and prior_admission.get("runnable") is False
    )
    if not (prior_is_prebuild or prior_is_sealed_successor):
        errors.append(f"{prefix}.protocol_amendment.prebuild_amendment_not_anchored")
    if prior_is_prebuild and (
        prior_admission_binding.get("path") != prebuild_anchor["admission_path"]
        or prior_admission_binding.get("sha256")
        != prebuild_anchor["admission_sha256"]
    ):
        errors.append(f"{prefix}.protocol_amendment.prebuild_admission_not_anchored")

    expected_prior_state = {
        "candidate_status": "implementation_pending",
        "implementation_authorized": True,
        "implementation_built": False,
        "implementation_admitted": False,
        "runnable": False,
        "backtest_admitted": False,
        "production_admitted": False,
    }
    if prebuild_amendment.get("prebuild_admission_state") != expected_prior_state:
        errors.append(f"{prefix}.protocol_amendment.prebuild_state_not_anchored")
    for field, expected in (
        ("status", "implementation_pending"),
        ("implementation_authorized", True),
        ("implementation_built", False),
        ("implementation_admitted", False),
        ("runnable", False),
        ("backtest_admitted", False),
        ("production_admitted", False),
    ):
        observed = prebuild_admission.get(field)
        if (
            (type(expected) is bool and observed is not expected)
            or (type(expected) is not bool and observed != expected)
        ):
            errors.append(
                f"{prefix}.protocol_amendment.prebuild_admission_{field}_mismatch"
            )

    expected_guard_protocol = dict(guard_protocol_binding)
    if guard_binding.get("protocol") != expected_guard_protocol:
        errors.append(f"{prefix}.protocol_amendment.implementation_guard_mismatch")
    guard_paths = [
        "factor_system/source_gate.py",
        "factor_system/tests/test_source_gate.py",
    ]
    guard_scope = (guard.get("authorized_implementation_scope") or {}).get(
        "files"
    )
    if not isinstance(guard_scope, list) or any(
        path not in guard_scope for path in guard_paths
    ):
        errors.append(f"{prefix}.implementation_guard.scope_invalid")
    _ordered_physical_identity(
        root,
        guard_binding.get("files"),
        guard_paths,
        f"{prefix}.protocol_amendment.implementation_guard.files",
        errors,
    )
    blind_guard_binding = amendment.get("blind_evaluation_guard")
    _verify_blind_guard_identity(
        root,
        blind_guard_binding,
        f"{prefix}.blind_evaluation_guard",
        errors,
    )
    prior_bindings = prebuild_amendment.get("input_identity_bindings") or {}
    active_bindings = amendment.get("input_identity_bindings") or {}
    prior_reader = prior_bindings.get("panel_reader_contract") or {}
    active_reader = active_bindings.get("panel_reader_contract") or {}
    expected_active_bindings = deepcopy(prior_bindings)
    if isinstance(expected_active_bindings.get("panel_reader_contract"), dict):
        expected_active_bindings["panel_reader_contract"]["sha256"] = (
            active_reader.get("sha256")
        )
    if prior_reader.get("sha256") == _PREBUILD_PANEL_READER_SHA256:
        if (
            prior_reader.get("path") != "factor_system/data.py"
            or active_reader.get("path") != prior_reader.get("path")
            or active_reader.get("sha256") == _PREBUILD_PANEL_READER_SHA256
            or active_bindings != expected_active_bindings
        ):
            errors.append(
                f"{prefix}.protocol_amendment.input_bindings_prebuild_drift"
            )
        expected_input_change = {
            "binding": "panel_reader_contract",
            "path": "factor_system/data.py",
            "old_sha256": _PREBUILD_PANEL_READER_SHA256,
            "new_sha256": active_reader.get("sha256"),
            "scope": "strict_content_addressed_panel_snapshot_loader_only",
            "blind_evaluation_guard": blind_guard_binding,
            "outcome_read": False,
        }
        if amendment.get("input_binding_change") != expected_input_change:
            errors.append(
                f"{prefix}.protocol_amendment.input_binding_change_mismatch"
            )
    elif (
        active_bindings != prior_bindings
        or amendment.get("input_binding_change") is not None
    ):
        errors.append(f"{prefix}.protocol_amendment.input_bindings_prebuild_drift")

    for field in (
        "base_protocol",
        "effective_lineage",
        "effective_input_feasibility",
        "evaluator_contract",
        "effective_frozen_tests",
    ):
        if amendment.get(field) != prebuild_amendment.get(field):
            errors.append(
                f"{prefix}.protocol_amendment.{field}_prebuild_drift"
            )

    implementation_spec = amendment.get("effective_implementation_spec") or {}
    prior_spec = prebuild_amendment.get("effective_implementation_spec") or {}
    for field in (
        "target_files",
        "test_files",
        "code_identity",
        "target_contract",
        "frozen_implementation_details",
        "independent_reference_contract",
    ):
        if implementation_spec.get(field) != prior_spec.get(field):
            errors.append(
                f"{prefix}.protocol_amendment.implementation_spec.{field}_prebuild_drift"
            )
    target_contract = implementation_spec.get("target_contract") or {}
    if target_contract.get("semantics_sha256") != authorized.get(
        "target_contract_sha256"
    ):
        errors.append(
            f"{prefix}.protocol_amendment.implementation_spec.target_anchor_mismatch"
        )
    if _canonical_json(implementation_spec.get("execution_permissions")) != (
        _canonical_json(_SEALED_EXECUTION_PERMISSIONS)
    ):
        errors.append(
            f"{prefix}.protocol_amendment.sealed_execution_permissions_invalid"
        )
    expected_local = prior_spec.get(
        "expected_local_implementation",
        implementation_spec.get("expected_local_implementation"),
    )
    expected_recipe = prior_spec.get(
        "expected_recipe", implementation_spec.get("expected_recipe"),
    )
    if _canonical_json(
        implementation_spec.get("expected_local_implementation")
    ) != _canonical_json(expected_local):
        errors.append(
            f"{prefix}.protocol_amendment.expected_local_implementation_mismatch"
        )
    if _canonical_json(implementation_spec.get("expected_recipe")) != (
        _canonical_json(expected_recipe)
    ):
        errors.append(f"{prefix}.protocol_amendment.expected_recipe_mismatch")
    _verify_prebuild_recipe_contract(
        str(candidate.get("candidate_id") or ""), protocol,
        implementation_spec, f"{prefix}.protocol_amendment", errors,
    )

    target_files = prior_spec.get("target_files")
    implementation_files = _ordered_physical_identity(
        root,
        implementation_spec.get("implementation_files"),
        target_files,
        f"{prefix}.protocol_amendment.implementation_files",
        errors,
    )
    test_files = prior_spec.get("test_files")
    test_identity = implementation_spec.get("test_identity") or {}
    test_bindings = _ordered_physical_identity(
        root,
        test_identity.get("files"),
        test_files,
        f"{prefix}.protocol_amendment.test_files",
        errors,
    )

    local = (candidate.get("formula") or {}).get("local_implementation")
    if _canonical_json(local) != _canonical_json(
        expected_local
    ):
        errors.append(f"{prefix}.formula.local_implementation_mismatch")

    implementation = candidate.get("implementation") or {}
    candidate_files = [
        {"path": item.get("path"), "sha256": item.get("sha256")}
        for item in _implementation_files(implementation)
    ]
    if candidate_files != implementation_files:
        errors.append(f"{prefix}.implementation.sealed_files_mismatch")

    verification = implementation_spec.get("synthetic_verification") or {}
    command = verification.get("command")
    passed = verification.get("passed")
    failed = verification.get("failed")
    if not isinstance(command, str) or not command.strip():
        errors.append(
            f"{prefix}.protocol_amendment.synthetic_verification.command_invalid"
        )
    if isinstance(passed, bool) or not isinstance(passed, int) or passed <= 0:
        errors.append(
            f"{prefix}.protocol_amendment.synthetic_verification.passed_invalid"
        )
    if isinstance(failed, bool) or failed != 0:
        errors.append(
            f"{prefix}.protocol_amendment.synthetic_verification.failed_invalid"
        )

    audit = _local_artifact(
        root,
        implementation.get("audit_path"),
        implementation.get("audit_sha256"),
        f"{prefix}.implementation_audit",
        errors,
    )
    if not audit:
        return
    if audit.get("schema_version") != 1:
        errors.append(f"{prefix}.implementation_audit.schema_version_invalid")
    for field in _SEALED_AUDIT_FALSE_FIELDS:
        if audit.get(field) is not False:
            errors.append(f"{prefix}.implementation_audit.{field}_must_be_false")
    if audit.get("implementation") != {"files": implementation_files}:
        errors.append(
            f"{prefix}.implementation_audit.implementation_files_mismatch"
        )
    audit_tests = audit.get("tests") or {}
    audit_test_bindings = _ordered_physical_identity(
        root,
        audit_tests.get("paths"),
        test_files,
        f"{prefix}.implementation_audit.test_files",
        errors,
    )
    if audit_test_bindings != test_bindings:
        errors.append(f"{prefix}.implementation_audit.test_identity_mismatch")
    if audit_tests.get("command") != command:
        errors.append(f"{prefix}.implementation_audit.test_command_mismatch")
    audit_passed = audit_tests.get("passed")
    if (
        isinstance(audit_passed, bool)
        or not isinstance(audit_passed, int)
        or audit_passed <= 0
        or audit_passed != passed
    ):
        errors.append(f"{prefix}.implementation_audit.test_passed_invalid")
    audit_failed = audit_tests.get("failed")
    if (
        isinstance(audit_failed, bool)
        or audit_failed != 0
        or audit_failed != failed
    ):
        errors.append(f"{prefix}.implementation_audit.test_failed_invalid")
    for field, expected in (
        ("prebuild_authorization", prebuild_anchor),
        ("target_contract", target_contract),
        ("evaluator_contract", amendment.get("evaluator_contract")),
        ("expected_local_implementation", expected_local),
        ("expected_recipe", expected_recipe),
    ):
        if _canonical_json(audit.get(field)) != _canonical_json(expected):
            errors.append(f"{prefix}.implementation_audit.{field}_mismatch")

    if admission.get("prebuild_authorization") != prebuild_anchor:
        errors.append(f"{prefix}.admission.prebuild_authorization_mismatch")
    if admission.get("blind_evaluation_guard") != blind_guard_binding:
        errors.append(f"{prefix}.admission.blind_evaluation_guard_mismatch")
    if admission.get("implementation_files") != implementation_files:
        errors.append(f"{prefix}.admission.implementation_files_mismatch")
    if admission.get("implementation_test_files") != test_bindings:
        errors.append(f"{prefix}.admission.implementation_test_files_mismatch")
    if _canonical_json(admission.get("expected_local_implementation")) != (
        _canonical_json(expected_local)
    ):
        errors.append(f"{prefix}.admission.expected_local_implementation_mismatch")
    if _canonical_json(admission.get("expected_recipe")) != _canonical_json(
        expected_recipe
    ):
        errors.append(f"{prefix}.admission.expected_recipe_mismatch")
    for field in _SEALED_AUDIT_FALSE_FIELDS:
        if admission.get(field) is not False:
            errors.append(f"{prefix}.admission.sealed_{field}_must_be_false")
    checks = admission.get("checks") or {}
    for check in (
        "synthetic_and_pit_tests_passed",
        "implementation_hashes_frozen",
        "independent_synthetic_reference_passed",
        "recipe_contract_frozen",
    ):
        if checks.get(check) is not True:
            errors.append(f"{prefix}.admission.{check}_not_true")
    if checks.get("blind_authorization_issued") is not False:
        errors.append(f"{prefix}.admission.blind_authorization_issued_must_be_false")
    if checks.get("implementation_not_started") is not False:
        errors.append(f"{prefix}.admission.implementation_not_started_must_be_false")
    if checks.get("source_gate_pytest_run") is not True:
        errors.append(f"{prefix}.admission.source_gate_pytest_run_not_true")
    if admission.get("next_allowed_action") != (
        "manual_one_time_blind_authorization_only"
    ):
        errors.append(f"{prefix}.admission.next_allowed_action_invalid")


def _verify_blind_rejection(
    root: Path,
    candidate: Mapping[str, Any],
    amendment: Mapping[str, Any],
    admission: Mapping[str, Any],
    prior_amendment: Mapping[str, Any],
    prior_admission: Mapping[str, Any],
    prefix: str,
    errors: list[str],
) -> None:
    """Verify the immutable outcome of the single authorized blind trial."""
    outcome = candidate.get("blind_evaluation")
    if not isinstance(outcome, Mapping):
        errors.append(f"{prefix}.blind_evaluation_not_object")
        return
    expected_fields = {
        "authorization", "evidence", "final_status", "fdr",
        "l0_l3_passed", "multiple_testing_family_id",
        "scientific_trial_consumed", "result_data_read", "disposition",
    }
    if set(outcome) != expected_fields:
        errors.append(f"{prefix}.blind_evaluation.fields_mismatch")
    family_id = candidate.get("multiple_testing_family_id")
    for field, expected in (
        ("final_status", "rejected"),
        ("l0_l3_passed", True),
        ("multiple_testing_family_id", family_id),
        ("scientific_trial_consumed", True),
        ("result_data_read", True),
        ("disposition", "permanently_archived_non_runnable"),
    ):
        if outcome.get(field) != expected:
            errors.append(f"{prefix}.blind_evaluation.{field}_mismatch")

    authorization_binding = outcome.get("authorization")
    if not isinstance(authorization_binding, Mapping):
        errors.append(f"{prefix}.blind_evaluation.authorization_not_object")
        authorization_binding = {}
    authorization_sha = authorization_binding.get("sha256")
    expected_authorization_path = (
        f"research/blind_authorizations/{authorization_sha}.json"
        if isinstance(authorization_sha, str) and _HEX64.fullmatch(
            authorization_sha
        ) else None
    )
    if (
        set(authorization_binding) != {"path", "sha256"}
        or authorization_binding.get("path") != expected_authorization_path
    ):
        errors.append(f"{prefix}.blind_evaluation.authorization_binding_invalid")
    authorization = _local_artifact(
        root,
        authorization_binding.get("path"),
        authorization_sha,
        f"{prefix}.blind_evaluation.authorization",
        errors,
    )

    evidence_binding = outcome.get("evidence")
    if not isinstance(evidence_binding, Mapping):
        errors.append(f"{prefix}.blind_evaluation.evidence_not_object")
        evidence_binding = {}
    evidence_sha = evidence_binding.get("sha256")
    expected_evidence_path = (
        "factor_system/registry/evidence/blind_runs/"
        f"{authorization_sha}/{evidence_sha}.json"
        if isinstance(authorization_sha, str)
        and _HEX64.fullmatch(authorization_sha)
        and isinstance(evidence_sha, str)
        and _HEX64.fullmatch(evidence_sha)
        else None
    )
    if (
        set(evidence_binding) != {"path", "sha256"}
        or evidence_binding.get("path") != expected_evidence_path
    ):
        errors.append(f"{prefix}.blind_evaluation.evidence_binding_invalid")
    evidence = _local_artifact(
        root,
        evidence_binding.get("path"),
        evidence_sha,
        f"{prefix}.blind_evaluation.evidence",
        errors,
    )
    if not authorization or not evidence:
        return

    authorization_candidate = authorization.get("candidate") or {}
    authorization_execution = authorization.get("execution") or {}
    authorization_permissions = authorization.get("permissions") or {}
    prior_chain = {
        "amendment_path": (amendment.get("prior_amendment") or {}).get("path"),
        "amendment_sha256": (amendment.get("prior_amendment") or {}).get(
            "sha256"
        ),
        "admission_path": (amendment.get("prior_admission") or {}).get("path"),
        "admission_sha256": (amendment.get("prior_admission") or {}).get(
            "sha256"
        ),
    }
    authorization_chain = authorization.get("chain") or {}
    if (
        authorization.get("schema_version") != 1
        or authorization.get("action") != "blind_discovery_validation_once"
        or authorization.get("max_executions") != 1
        or authorization.get("manual_invocation_only") is not True
        or authorization_candidate.get("id") != candidate.get("candidate_id")
        or authorization_candidate.get("multiple_testing_family_id") != family_id
        or authorization_execution.get("fail_status") != "rejected"
        or authorization_execution.get("max_scientific_trials") != 1
        or authorization_permissions.get("production_allowed") is not False
        or authorization_permissions.get("paid_api_allowed") is not False
        or authorization_permissions.get("automatic_trading_allowed") is not False
        or any(
            authorization_chain.get(field) != value
            for field, value in prior_chain.items()
        )
    ):
        errors.append(f"{prefix}.blind_evaluation.authorization_contract_invalid")

    evidence_result = evidence.get("result") or {}
    l3 = evidence_result
    l3_fdr = evidence.get("l3_fdr") or {}
    fdr = outcome.get("fdr")
    expected_fdr = {
        "q": (authorization.get("evaluator") or {}).get(
            "config_values", {}
        ).get("FDR_Q"),
        "p_value": l3_fdr.get("p_value"),
        "reject": False,
    }
    if fdr != expected_fdr:
        errors.append(f"{prefix}.blind_evaluation.fdr_mismatch")
    if (
        evidence.get("authorization_path") != authorization_binding.get("path")
        or evidence.get("authorization_sha256") != authorization_sha
        or evidence.get("candidate_id") != candidate.get("candidate_id")
        or evidence.get("candidate_research_record_sha256")
        != authorization_candidate.get("research_record_sha256")
        or evidence.get("evaluator_fingerprint")
        != (authorization.get("evaluator") or {}).get("fingerprint")
        or evidence.get("implementation_audit_sha256")
        != authorization_chain.get("implementation_audit_sha256")
        or evidence.get("panel") != {
            "path": (authorization.get("input") or {}).get("panel_cache_path"),
            "sha256": (authorization.get("input") or {}).get(
                "panel_cache_sha256"
            ),
            "key": (authorization.get("input") or {}).get("panel_cache_key"),
            "data_key": (authorization.get("input") or {}).get("panel_data_key"),
        }
        or evidence.get("final_status") != "rejected"
        or evidence.get("production_unlocked") is not False
        or any(
            (evidence_result.get(stage) or {}).get("passed") is not True
            for stage in ("L0", "L1", "L2")
        )
        or l3.get("passed") is not True
        or l3_fdr.get("reject") is not False
        or l3_fdr.get("p_value") != l3.get("validation_p_value")
    ):
        errors.append(f"{prefix}.blind_evaluation.evidence_contract_invalid")

    preserved_fields = (
        "base_protocol", "effective_input_feasibility", "evaluator_contract",
        "effective_frozen_tests", "effective_implementation_spec",
        "input_identity_bindings", "prebuild_authorization",
        "implementation_guard", "blind_evaluation_guard",
        "input_binding_change",
    )
    for field in preserved_fields:
        if amendment.get(field) != prior_amendment.get(field):
            errors.append(f"{prefix}.protocol_amendment.{field}_outcome_drift")
    expected_lineage = deepcopy(prior_amendment.get("effective_lineage") or {})
    expected_lineage["current_candidate_return_result_read"] = True
    if (
        amendment.get("effective_lineage") != expected_lineage
        or candidate.get("lineage") != expected_lineage
    ):
        errors.append(f"{prefix}.protocol_amendment.outcome_lineage_mismatch")
    if prior_admission.get("status") != _SEALED_IMPLEMENTATION_STATUS:
        errors.append(f"{prefix}.protocol_amendment.prior_not_implementation_built")
    if amendment.get("blind_evaluation") != outcome:
        errors.append(f"{prefix}.protocol_amendment.blind_evaluation_mismatch")
    if admission.get("blind_evaluation") != outcome:
        errors.append(f"{prefix}.admission.blind_evaluation_mismatch")
    if amendment.get("blind_rejection_state") != _BLIND_REJECTION_STATE:
        errors.append(f"{prefix}.protocol_amendment.blind_rejection_state_mismatch")
    if admission.get("next_allowed_action") != "none_candidate_archived":
        errors.append(f"{prefix}.admission.next_allowed_action_invalid")


def load_catalog(path: Path = CATALOG_PATH) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceCatalogError(f"source catalog unreadable: {exc}") from exc
    if not isinstance(value, dict):
        raise SourceCatalogError("source catalog must be a JSON object")
    return value


def _catalog(value: Mapping[str, Any] | None) -> dict[str, Any]:
    return dict(value) if value is not None else load_catalog()


def _sources(catalog: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for source in catalog.get("sources") or []:
        if isinstance(source, dict) and isinstance(source.get("source_id"), str):
            result[source["source_id"]] = source
    return result


def _candidate_by_id(
    candidate_id: str,
    catalog: Mapping[str, Any],
) -> dict[str, Any] | None:
    for candidate in catalog.get("candidates") or []:
        if (
            isinstance(candidate, dict)
            and candidate.get("candidate_id") == candidate_id
        ):
            return candidate
    return None


def _supersession_for(
    candidate_id: str,
    catalog: Mapping[str, Any],
) -> dict[str, Any] | None:
    for item in catalog.get("superseded_non_outcome_trials") or []:
        if isinstance(item, dict) and item.get("candidate_id") == candidate_id:
            return item
    return None


def _archive_by_id(
    archive_id: str,
    catalog: Mapping[str, Any],
) -> dict[str, Any] | None:
    for archive in catalog.get("archived_prior_tests") or []:
        if isinstance(archive, dict) and archive.get("archive_id") == archive_id:
            return archive
    return None


def _verify_prior_trial_identity(
    trial: Mapping[str, Any], prefix: str,
) -> list[str]:
    errors: list[str] = []
    if not isinstance(trial.get("candidate_id"), str) or not trial[
        "candidate_id"
    ].strip():
        errors.append(f"{prefix}.candidate_id_invalid")
    if not isinstance(trial.get("status"), str) or not trial["status"].strip():
        errors.append(f"{prefix}.status_invalid")
    if not isinstance(trial.get("result_data_read"), bool):
        errors.append(f"{prefix}.result_data_read_invalid")
    for field in (
        "research_record_sha256", "protocol_sha256", "admission_sha256",
    ):
        value = trial.get(field)
        if not isinstance(value, str) or not _HEX64.fullmatch(value):
            errors.append(f"{prefix}.{field}_invalid")
    return errors


def _verify_lifecycle_resolution(
    trial: Mapping[str, Any], root: Path, prefix: str,
) -> list[str]:
    """Verify an optional immutable explanation of a closed trial's lifecycle."""
    path = trial.get("lifecycle_resolution_path")
    digest = trial.get("lifecycle_resolution_sha256")
    if path is None and digest is None:
        return []
    errors: list[str] = []
    _, body = _verified_local_file(
        root, path, digest, f"{prefix}.lifecycle_resolution", errors,
        keep_bytes=True,
    )
    if body is None:
        return errors
    try:
        resolution = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return [*errors, f"{prefix}.lifecycle_resolution_json_invalid"]
    if not isinstance(resolution, Mapping):
        return [*errors, f"{prefix}.lifecycle_resolution_not_object"]
    if resolution.get("candidate_id") != trial.get("candidate_id"):
        errors.append(f"{prefix}.lifecycle_resolution_candidate_id_mismatch")
    if resolution.get("multiple_testing_family_id") != trial.get(
        "multiple_testing_family_id",
    ):
        errors.append(f"{prefix}.lifecycle_resolution_family_mismatch")
    expected = {
        "protocol": {
            "path": trial.get("protocol_path"),
            "sha256": trial.get("protocol_sha256"),
        },
        "admission": {
            "path": trial.get("admission_path"),
            "sha256": trial.get("admission_sha256"),
        },
        "research_result": {
            "path": trial.get("research_result_path"),
            "sha256": trial.get("research_result_sha256"),
        },
    }
    if resolution.get("immutable_original_artifacts") != expected:
        errors.append(f"{prefix}.lifecycle_resolution_originals_mismatch")
    timeline = resolution.get("status_timeline")
    current = resolution.get("resolution")
    if not isinstance(timeline, Mapping) or (
        timeline.get("effective_current_status") != "permanently_closed"
    ):
        errors.append(f"{prefix}.lifecycle_resolution_status_invalid")
    if not isinstance(current, Mapping) or any(
        current.get(field) is not False
        for field in (
            "runnable", "rerun_allowed", "successor_created",
            "production_or_recommendation_allowed",
        )
    ):
        errors.append(f"{prefix}.lifecycle_resolution_authorization_invalid")
    return errors


def _verify_current_lineage(
    lineage: object,
    catalog: Mapping[str, Any],
    prefix: str,
    candidate_id: str,
    family_id: str,
    *,
    result_read: bool,
) -> list[str]:
    if not isinstance(lineage, dict):
        return [f"{prefix}.lineage_not_object"]
    errors: list[str] = []
    if lineage.get("current_candidate_return_result_read") is not result_read:
        errors.append(f"{prefix}.lineage.current_result_read_mismatch")
    if any(_VERSIONED_CURRENT_RESULT.fullmatch(str(field)) for field in lineage):
        errors.append(f"{prefix}.lineage.versioned_current_result_forbidden")
    if lineage.get("same_multiple_testing_family") is not True:
        errors.append(f"{prefix}.lineage.multiple_testing_family_not_preserved")
    lineage_family = lineage.get("multiple_testing_family_id")
    if lineage_family is not None and lineage_family != family_id:
        errors.append(f"{prefix}.lineage.multiple_testing_family_id_mismatch")
    prior_trials = lineage.get("prior_trials")
    if not isinstance(prior_trials, list):
        errors.append(f"{prefix}.lineage.prior_trials_missing")
        prior_trials = []

    historical = [
        record
        for collection in _PRIOR_TRIAL_COLLECTIONS
        for record in catalog.get(collection) or []
        if isinstance(record, Mapping)
        and record.get("multiple_testing_family_id") == family_id
    ]
    ancestor_ids: set[str] = set()
    frontier = {candidate_id}
    while frontier:
        parents = {
            str(record.get("candidate_id"))
            for record in historical
            if str(record.get("superseded_by")) in frontier
            and str(record.get("candidate_id")) not in ancestor_ids
            and str(record.get("candidate_id")) != candidate_id
        }
        ancestor_ids.update(parents)
        frontier = parents

    catalog_trials: list[Mapping[str, Any]] = []
    for collection in _PRIOR_TRIAL_COLLECTIONS:
        records = catalog.get(collection)
        if not isinstance(records, list):
            errors.append(f"{prefix}.lineage.{collection}_not_list")
            continue
        records = [
            record for record in records
            if isinstance(record, Mapping)
            and record.get("multiple_testing_family_id") == family_id
            and str(record.get("candidate_id")) in ancestor_ids
        ]
        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                errors.append(
                    f"{prefix}.lineage.{collection}[{index}]_not_object"
                )
                continue
            errors.extend(_verify_prior_trial_identity(
                record, f"{prefix}.lineage.{collection}[{index}]",
            ))
            catalog_trials.append(record)

    if len(prior_trials) != len(catalog_trials):
        errors.append(f"{prefix}.lineage.prior_trials_count_mismatch")
    seen: set[str] = set()
    for index, trial in enumerate(prior_trials):
        item_prefix = f"{prefix}.lineage.prior_trials[{index}]"
        if not isinstance(trial, Mapping):
            errors.append(f"{item_prefix}_not_object")
            continue
        errors.extend(_verify_prior_trial_identity(trial, item_prefix))
        prior_id = trial.get("candidate_id")
        if isinstance(prior_id, str):
            if prior_id in seen:
                errors.append(f"{item_prefix}.candidate_id_duplicate")
            seen.add(prior_id)
        matches = [
            record for record in catalog_trials
            if record.get("candidate_id") == prior_id
        ]
        if len(matches) != 1:
            errors.append(f"{item_prefix}.catalog_cross_link_missing")
            continue
        canonical = matches[0]
        trial_family = trial.get("multiple_testing_family_id", family_id)
        if trial_family != family_id:
            errors.append(f"{item_prefix}.multiple_testing_family_id_mismatch")
        for field in _PRIOR_TRIAL_IDENTITY_FIELDS:
            if trial.get(field) != canonical.get(field):
                errors.append(f"{item_prefix}.{field}_mismatch")
    return errors


def record_for_candidate(
    candidate: Mapping[str, Any] | str,
    catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    active_catalog = _catalog(catalog)
    if isinstance(candidate, str):
        resolved = _candidate_by_id(candidate, active_catalog)
        candidate_value = resolved or {"candidate_id": candidate}
    else:
        candidate_value = dict(candidate)
    source_map = _sources(active_catalog)
    source_ids = candidate_value.get("source_ids") or []
    source_records = [
        deepcopy(source_map[source_id])
        for source_id in source_ids
        if source_id in source_map
    ]
    return {
        "schema_version": 1,
        "record_type": "canonical_factor_research_record",
        "catalog_id": active_catalog.get("catalog_id"),
        "catalog_policy": deepcopy(active_catalog.get("catalog_policy") or {}),
        "candidate": deepcopy(candidate_value),
        "sources": source_records,
        "house_policies": deepcopy(
            active_catalog.get("house_policies") or []
        ),
    }


def _archive_record(
    archive: Mapping[str, Any],
    catalog: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "record_type": "canonical_factor_archive_record",
        "catalog_id": catalog.get("catalog_id"),
        "archive": deepcopy(dict(archive)),
    }


def _verify_source(
    source: object,
    allowed_authorities: set[str],
) -> list[str]:
    if not isinstance(source, dict):
        return ["source_not_object"]
    source_id = source.get("source_id")
    prefix = f"source[{source_id or '?'}]"
    errors: list[str] = []
    if not isinstance(source_id, str) or not source_id.strip():
        errors.append(f"{prefix}.source_id_missing")
    authority = source.get("authority")
    if authority not in allowed_authorities:
        errors.append(f"{prefix}.authority_not_allowed")
    license_value = source.get("license")
    if not isinstance(license_value, dict):
        errors.append(f"{prefix}.license_missing")
        license_value = {}
    paper = source.get("paper")
    if not isinstance(paper, dict):
        errors.append(f"{prefix}.primary_paper_missing")
        paper = {}

    if authority == "official_research_repository":
        repository = source.get("repository")
        if not (
            isinstance(repository, str)
            and repository.startswith("https://github.com/")
        ):
            errors.append(f"{prefix}.official_repository_invalid")
        if not isinstance(source.get("commit"), str) or not _HEX40.fullmatch(
            source["commit"]
        ):
            errors.append(f"{prefix}.commit_invalid")
        if not _safe_relative_path(source.get("source_path")):
            errors.append(f"{prefix}.source_path_invalid")
        if not isinstance(source.get("source_git_blob_sha1"), str) or not (
            _HEX40.fullmatch(source["source_git_blob_sha1"])
        ):
            errors.append(f"{prefix}.source_git_blob_sha1_invalid")
        if not isinstance(license_value.get("spdx"), str) or not (
            license_value["spdx"].strip()
        ):
            errors.append(f"{prefix}.license_spdx_missing")
        if not _safe_relative_path(license_value.get("path")):
            errors.append(f"{prefix}.license_path_invalid")
        if not isinstance(license_value.get("git_blob_sha1"), str) or not (
            _HEX40.fullmatch(license_value["git_blob_sha1"])
        ):
            errors.append(f"{prefix}.license_git_blob_sha1_invalid")
        if paper.get("authority") not in allowed_authorities:
            errors.append(f"{prefix}.paper_authority_not_allowed")
        if not (
            isinstance(paper.get("official_url"), str)
            and paper["official_url"].startswith("https://")
        ):
            errors.append(f"{prefix}.paper_official_url_invalid")
    elif authority == "primary_paper":
        if source.get("no_repository_required") is not True:
            errors.append(f"{prefix}.no_repository_required_not_true")
        for field in (
            "repository", "commit", "source_path", "source_git_blob_sha1",
        ):
            if source.get(field) is not None:
                errors.append(f"{prefix}.{field}_forbidden_for_primary_paper")
        if paper.get("authority") != "primary_paper":
            errors.append(f"{prefix}.paper_authority_invalid")
        if not isinstance(paper.get("title"), str) or not paper["title"].strip():
            errors.append(f"{prefix}.paper_title_missing")
        authors = paper.get("authors")
        if (
            not isinstance(authors, list)
            or not authors
            or any(not isinstance(author, str) or not author.strip()
                   for author in authors)
        ):
            errors.append(f"{prefix}.paper_authors_invalid")
        if not isinstance(paper.get("year"), int):
            errors.append(f"{prefix}.paper_year_invalid")
        doi = paper.get("doi")
        if not isinstance(doi, str) or not doi.startswith("10."):
            errors.append(f"{prefix}.paper_doi_invalid")
        if not (
            isinstance(paper.get("primary_url"), str)
            and paper["primary_url"].startswith("https://")
        ):
            errors.append(f"{prefix}.paper_primary_url_invalid")
        if not isinstance(license_value.get("spdx"), str) or not (
            license_value["spdx"].strip()
        ):
            errors.append(f"{prefix}.license_spdx_missing")
        if not (
            isinstance(license_value.get("license_url"), str)
            and license_value["license_url"].startswith("https://")
        ):
            errors.append(f"{prefix}.license_url_invalid")
        if license_value.get("copyright_use") != "clean_room_formula_only":
            errors.append(f"{prefix}.copyright_use_invalid")
        if license_value.get("source_text_or_code_copied") is not False:
            errors.append(f"{prefix}.source_text_or_code_copied_forbidden")
    else:
        # Preserve a closed error surface for unknown authority values.
        errors.append(f"{prefix}.source_schema_unknown")
    if source.get("uses_paid_api") is not False:
        errors.append(f"{prefix}.paid_api_forbidden")
    if source.get("result_data_read") is not False:
        errors.append(f"{prefix}.result_data_read_forbidden")
    return errors


def _verify_candidate_fields(
    candidate: Mapping[str, Any],
    catalog: Mapping[str, Any],
    root: Path,
) -> tuple[list[str], list[str]]:
    candidate_id = candidate.get("candidate_id") or "?"
    prefix = f"candidate[{candidate_id}]"
    errors: list[str] = []
    warnings: list[str] = []
    missing = sorted(_REQUIRED_CANDIDATE_FIELDS - set(candidate))
    errors.extend(f"{prefix}.{field}_missing" for field in missing)
    policy = catalog.get("catalog_policy") or {}
    evidence_bases = set(policy.get("allowed_evidence_bases") or [])
    statuses = set(policy.get("allowed_candidate_statuses") or [])
    directions = set(policy.get("allowed_directions") or [])
    runnable_statuses = set(policy.get("runnable_statuses") or [])
    if candidate.get("evidence_basis") not in evidence_bases:
        errors.append(f"{prefix}.evidence_basis_invalid")
    source_map = _sources(catalog)
    source_ids = candidate.get("source_ids")
    if not isinstance(source_ids, list) or not source_ids:
        errors.append(f"{prefix}.source_ids_empty")
        source_ids = []
    elif any(not isinstance(source_id, str) for source_id in source_ids):
        errors.append(f"{prefix}.source_ids_invalid")
    elif len(source_ids) != len(set(source_ids)):
        errors.append(f"{prefix}.source_ids_duplicate")
    allowed_authorities = set(policy.get("allowed_source_authorities") or [])
    checked_sources: set[str] = set()
    for source_id in source_ids:
        if not isinstance(source_id, str) or source_id not in source_map:
            errors.append(f"{prefix}.source_id_unknown:{source_id}")
        elif source_id not in checked_sources:
            errors.extend(_verify_source(source_map[source_id], allowed_authorities))
            checked_sources.add(source_id)
    for field in (
        "mechanism",
        "formula",
        "direction",
        "data",
        "point_in_time",
    ):
        value = candidate.get(field)
        if not isinstance(value, dict):
            errors.append(f"{prefix}.{field}_not_object")
        elif value.get("basis") not in evidence_bases:
            errors.append(f"{prefix}.{field}.basis_invalid")
    mechanism = candidate.get("mechanism")
    if not isinstance(mechanism, dict):
        mechanism = {}
    if not isinstance(mechanism.get("statement"), str) or not (
        mechanism["statement"].strip()
    ):
        errors.append(f"{prefix}.mechanism.statement_missing")
    formula = candidate.get("formula")
    if not isinstance(formula, dict):
        formula = {}
    formula_kind = formula.get("kind", "regression")
    characteristic_kinds = {"characteristic", "ratio_characteristic"}
    required_formula_fields = (
        ("value",)
        if formula_kind in characteristic_kinds
        else ("restricted_regression", "unrestricted_regression", "value")
    )
    for field in required_formula_fields:
        if not isinstance(formula.get(field), str) or not formula[field].strip():
            errors.append(f"{prefix}.formula.{field}_missing")
    if formula_kind in characteristic_kinds:
        for field in ("restricted_regression", "unrestricted_regression"):
            if formula.get(field) is not None:
                errors.append(f"{prefix}.formula.{field}_forbidden")
    elif formula_kind != "regression":
        errors.append(f"{prefix}.formula.kind_invalid")
    direction = candidate.get("direction")
    if not isinstance(direction, dict):
        direction = {}
    if direction.get("value") not in directions:
        errors.append(f"{prefix}.direction.value_invalid")
    data = candidate.get("data")
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("required_semantics"), list) or not (
        data["required_semantics"]
    ):
        errors.append(f"{prefix}.data.required_semantics_empty")
    access_modes = data.get("allowed_access_modes") or []
    if not isinstance(access_modes, list) or any(
        not isinstance(mode, str) for mode in access_modes
    ):
        errors.append(f"{prefix}.data.allowed_access_modes_invalid")
    elif any("paid" in mode.casefold() for mode in access_modes):
        errors.append(f"{prefix}.data.paid_access_mode_forbidden")
    if candidate.get("paid_api_required") is not False:
        errors.append(f"{prefix}.paid_api_required_must_be_false")
    if data.get("paid_api_required") is not False:
        errors.append(f"{prefix}.data.paid_api_required_must_be_false")
    status = candidate.get("status")
    result_read = status == _BLIND_REJECTED_STATUS
    if candidate.get("result_data_read") is not result_read:
        errors.append(f"{prefix}.result_data_read_mismatch")
    if data.get("result_data_read") is not False:
        errors.append(f"{prefix}.data.result_data_read_must_be_false")
    if data.get("upstream_return_data_used") is not False:
        errors.append(f"{prefix}.data.upstream_return_data_used_must_be_false")
    point_in_time = candidate.get("point_in_time")
    if not isinstance(point_in_time, dict):
        point_in_time = {}
    if point_in_time.get("future_data_allowed") is not False:
        errors.append(f"{prefix}.point_in_time.future_data_allowed")
    if not isinstance(point_in_time.get("formation_rule"), str) or not (
        point_in_time["formation_rule"].strip()
    ):
        errors.append(f"{prefix}.point_in_time.formation_rule_missing")
    counter_evidence = candidate.get("counter_evidence")
    if not isinstance(counter_evidence, list) or not counter_evidence:
        errors.append(f"{prefix}.counter_evidence_empty")
    else:
        for index, item in enumerate(counter_evidence):
            if not isinstance(item, dict):
                errors.append(f"{prefix}.counter_evidence[{index}]_not_object")
                continue
            if item.get("basis") not in evidence_bases:
                errors.append(f"{prefix}.counter_evidence[{index}].basis_invalid")
            if not isinstance(item.get("risk"), str) or not item["risk"].strip():
                errors.append(f"{prefix}.counter_evidence[{index}].risk_missing")
    license_value = candidate.get("license")
    if not isinstance(license_value, dict):
        errors.append(f"{prefix}.license_not_object")
    else:
        source_id = license_value.get("source_id")
        source = source_map.get(source_id)
        if source is None or source_id not in source_ids:
            errors.append(f"{prefix}.license.source_id_invalid")
        elif license_value.get("spdx") != (
            source.get("license") or {}
        ).get("spdx"):
            errors.append(f"{prefix}.license.spdx_mismatch")
        if license_value.get("reuse_mode") not in {
            "independent_formula_implementation_no_source_copy",
            "licensed_source_reuse",
        }:
            errors.append(f"{prefix}.license.reuse_mode_invalid")
    runnable = candidate.get("runnable")
    if status not in statuses:
        errors.append(f"{prefix}.status_invalid")
    if not isinstance(runnable, bool):
        errors.append(f"{prefix}.runnable_not_boolean")
    elif runnable != (status in runnable_statuses):
        errors.append(f"{prefix}.status_runnable_inconsistent")
    if status == "implementation_pending":
        warnings.append(f"{prefix}.implementation_pending_not_runnable")
    if status == "data_pending":
        warnings.append(f"{prefix}.data_pending_not_runnable")
    if status == _SEALED_IMPLEMENTATION_STATUS:
        warnings.append(f"{prefix}.implementation_built_not_runnable")
    if status == _BLIND_REJECTED_STATUS:
        warnings.append(f"{prefix}.blind_rejected_permanently_archived")
    supersession = _supersession_for(str(candidate_id), catalog)
    if supersession is not None:
        warnings.append(f"{prefix}.superseded_non_outcome_trial_not_runnable")
    declared_family_id = candidate.get("multiple_testing_family_id")
    historical_family_id = (
        supersession.get("multiple_testing_family_id")
        if supersession is not None else None
    )
    if supersession is None:
        family_id = declared_family_id
        if not isinstance(family_id, str) or not family_id.strip():
            errors.append(f"{prefix}.multiple_testing_family_id_missing")
            family_id = ""
    else:
        family_id = historical_family_id
        if not isinstance(family_id, str) or not family_id.strip():
            errors.append(f"{prefix}.multiple_testing_family_id_missing")
            family_id = ""
        if declared_family_id is not None and declared_family_id != family_id:
            errors.append(f"{prefix}.multiple_testing_family_id_mismatch")
    input_feasibility = candidate.get("input_feasibility")
    feasibility_passed: bool | None = None
    feasibility: dict[str, Any] = {}
    input_integrity: dict[str, Any] = {}
    if input_feasibility is not None:
        if not isinstance(input_feasibility, dict):
            errors.append(f"{prefix}.input_feasibility_not_object")
        else:
            feasibility = _local_artifact(
                root,
                input_feasibility.get("path"),
                input_feasibility.get("sha256"),
                f"{prefix}.input_feasibility",
                errors,
            )
            if feasibility:
                decision = feasibility.get("decision")
                if not isinstance(decision, dict):
                    errors.append(
                        f"{prefix}.input_feasibility.decision_not_object"
                    )
                    decision = {}
                if feasibility.get("candidate_id") != candidate_id:
                    errors.append(
                        f"{prefix}.input_feasibility.candidate_id_mismatch"
                    )
                if feasibility.get("result_data_read") is not False:
                    errors.append(
                        f"{prefix}.input_feasibility.result_data_read_forbidden"
                    )
                if feasibility.get("candidate_outcome_computed") is not False:
                    errors.append(
                        f"{prefix}.input_feasibility.outcome_computed_forbidden"
                    )
                if feasibility.get("input_integrity_audit") is not None:
                    for field in (
                        "candidate_signal_computed",
                        "price_delay_computed",
                        "ic_or_gate_computed",
                        "mining_or_backtest_run",
                        "paid_api_used",
                    ):
                        if feasibility.get(field) is not False:
                            errors.append(
                                f"{prefix}.input_feasibility.{field}_must_be_false"
                            )
                feasibility_passed = decision.get("input_feasibility_passed")
                if (status in {"data_pending", "implementation_pending"}
                        and type(feasibility_passed) is not bool):
                    errors.append(
                        f"{prefix}.input_feasibility.decision_invalid"
                    )
                elif (status not in {"data_pending", "implementation_pending"}
                      and feasibility_passed is not True):
                    errors.append(
                        f"{prefix}.input_feasibility.input_not_feasible"
                    )
                integrity_binding = feasibility.get("input_integrity_audit")
                if integrity_binding is not None:
                    if not isinstance(integrity_binding, dict):
                        errors.append(
                            f"{prefix}.input_feasibility.input_integrity_not_object"
                        )
                    else:
                        input_integrity = _local_artifact(
                            root,
                            integrity_binding.get("path"),
                            integrity_binding.get("sha256"),
                            f"{prefix}.input_feasibility.input_integrity",
                            errors,
                        )
                        if input_integrity:
                            if input_integrity.get("status") != integrity_binding.get(
                                "status"
                            ):
                                errors.append(
                                    f"{prefix}.input_feasibility.input_integrity_status_mismatch"
                                )
                            if input_integrity.get("input_integrity_passed") is not True:
                                errors.append(
                                    f"{prefix}.input_feasibility.input_integrity_not_passed"
                                )
                            for field in (
                                "result_data_read",
                                "candidate_signal_computed",
                                "candidate_outcome_computed",
                                "price_delay_computed",
                                "ic_or_gate_computed",
                                "mining_or_backtest_run",
                            ):
                                if input_integrity.get(field) is not False:
                                    errors.append(
                                        f"{prefix}.input_feasibility.input_integrity_{field}_forbidden"
                                    )
    lineage = candidate.get("lineage")
    uses_current_lineage = supersession is None or (
        isinstance(lineage, dict) and "prior_trials" in lineage
    )
    if uses_current_lineage:
        if (supersession is None and isinstance(lineage, dict)
                and lineage.get("multiple_testing_family_id") != family_id):
            errors.append(
                f"{prefix}.lineage.multiple_testing_family_id_mismatch"
            )
        errors.extend(_verify_current_lineage(
            lineage,
            catalog,
            prefix,
            str(candidate_id),
            str(family_id),
            result_read=status == _BLIND_REJECTED_STATUS,
        ))
        lineage = lineage if isinstance(lineage, dict) else {}
    elif lineage is not None:
        if not isinstance(lineage, dict):
            errors.append(f"{prefix}.lineage_not_object")
            lineage = {}
        prior_id = lineage.get("prior_trial_id")
        if not isinstance(prior_id, str) or not prior_id:
            errors.append(f"{prefix}.lineage.prior_trial_id_missing")
        if lineage.get("prior_trial_outcome_known") is not True:
            errors.append(f"{prefix}.lineage.prior_trial_outcome_known_not_true")
        if lineage.get("current_v2_return_result_read") is not False:
            errors.append(f"{prefix}.lineage.current_result_must_be_unread")
        if lineage.get("same_multiple_testing_family") is not True:
            errors.append(f"{prefix}.lineage.multiple_testing_family_not_preserved")
        prior_trials = [
            trial for trial in catalog.get("contaminated_trials") or []
            if isinstance(trial, dict) and trial.get("candidate_id") == prior_id
        ]
        if len(prior_trials) != 1:
            errors.append(f"{prefix}.lineage.contaminated_trial_not_preserved")
        elif (prior_trials[0].get("result_data_read") is not True
              or prior_trials[0].get("runnable") is not False):
            errors.append(f"{prefix}.lineage.contaminated_trial_invalid")
    implementation_frozen = runnable is True or status in {
        "data_pending", _SEALED_IMPLEMENTATION_STATUS,
        _BLIND_REJECTED_STATUS,
    }
    terminal_archived_non_runnable = runnable is False and (
        supersession is not None or status == _BLIND_REJECTED_STATUS
    )
    if implementation_frozen:
        local = formula.get("local_implementation")
        required_local = {
            "factor_id", "family", "formula", "params",
            "data_requires", "direction",
        }
        if not isinstance(local, dict):
            errors.append(f"{prefix}.formula.local_implementation_not_object")
        else:
            missing_local = sorted(required_local - set(local))
            errors.extend(
                f"{prefix}.formula.local_implementation.{field}_missing"
                for field in missing_local
            )
            if local.get("factor_id") != candidate_id:
                errors.append(
                    f"{prefix}.formula.local_implementation.factor_id_mismatch"
                )
            if not isinstance(local.get("family"), str) or not local.get("family"):
                errors.append(f"{prefix}.formula.local_implementation.family_invalid")
            if not isinstance(local.get("formula"), str) or not local.get("formula"):
                errors.append(f"{prefix}.formula.local_implementation.formula_invalid")
            if not isinstance(local.get("params"), dict):
                errors.append(f"{prefix}.formula.local_implementation.params_invalid")
            if (not isinstance(local.get("data_requires"), list)
                    or not local.get("data_requires")
                    or any(not isinstance(value, str)
                           for value in local.get("data_requires", []))):
                errors.append(
                    f"{prefix}.formula.local_implementation.data_requires_invalid"
                )
            if not isinstance(local.get("direction"), str) or not local.get(
                "direction"
            ):
                errors.append(
                    f"{prefix}.formula.local_implementation.direction_invalid"
                )
        if point_in_time.get("local_audit_status") != "passed":
            errors.append(f"{prefix}.point_in_time.local_audit_not_passed")
        implementation = candidate.get("implementation")
        if not isinstance(implementation, dict):
            errors.append(f"{prefix}.implementation_not_object")
        else:
            # A permanently superseded, non-runnable trial keeps its original
            # code identity in the signed audit after the shared live module
            # advances to a new candidate version. Only that terminal state
            # may skip hashing the live path; its audit and admission remain
            # fully verified below.
            if not terminal_archived_non_runnable:
                _verify_implementation_files(root, implementation, prefix, errors)
            audit = _local_artifact(
                root,
                implementation.get("audit_path"),
                implementation.get("audit_sha256"),
                f"{prefix}.implementation_audit",
                errors,
            )
            if audit:
                audited_code = audit.get("implementation") or {}
                tests = audit.get("tests") or {}
                if audit.get("candidate_id") != candidate_id:
                    errors.append(
                        f"{prefix}.implementation_audit.candidate_id_mismatch"
                    )
                if audit.get("status") != "passed":
                    errors.append(f"{prefix}.implementation_audit.status_not_passed")
                if audit.get("result_data_read") is not False:
                    errors.append(
                        f"{prefix}.implementation_audit.result_data_read_forbidden"
                    )
                if audit.get("paid_api_used") is not False:
                    errors.append(
                        f"{prefix}.implementation_audit.paid_api_used_forbidden"
                    )
                if audited_code != _implementation_audit_identity(implementation):
                    errors.append(
                        f"{prefix}.implementation_audit.code_identity_mismatch"
                    )
                if tests.get("failed") != 0 or not isinstance(
                    tests.get("passed"), int
                ) or tests.get("passed", 0) <= 0:
                    errors.append(
                        f"{prefix}.implementation_audit.tests_not_passed"
                    )
    preregistration = candidate.get("preregistration")
    if not isinstance(preregistration, dict):
        errors.append(f"{prefix}.preregistration_not_object")
        return errors, warnings
    protocol = _local_artifact(
        root,
        preregistration.get("protocol_path"),
        preregistration.get("protocol_sha256"),
        f"{prefix}.protocol",
        errors,
    )
    admission = _local_artifact(
        root,
        preregistration.get("admission_path"),
        preregistration.get("admission_sha256"),
        f"{prefix}.admission",
        errors,
    )
    amendment = {}
    if (
        preregistration.get("amendment_path") is not None
        or preregistration.get("amendment_sha256") is not None
    ):
        amendment = _local_artifact(
            root,
            preregistration.get("amendment_path"),
            preregistration.get("amendment_sha256"),
            f"{prefix}.protocol_amendment",
            errors,
        )
    if (
        not amendment
        and supersession is None
        and status == "implementation_pending"
        and isinstance(lineage, dict)
        and any(
            isinstance(trial, Mapping)
            and trial.get("result_data_read") is True
            for trial in (lineage.get("prior_trials") or [])
        )
    ):
        errors.append(f"{prefix}.protocol_amendment.required")
    if protocol:
        if protocol.get("protocol_id") != candidate_id:
            errors.append(f"{prefix}.protocol.candidate_id_mismatch")
        if protocol.get("status") != preregistration.get("protocol_status"):
            errors.append(f"{prefix}.protocol.status_mismatch")
        if protocol.get("result_data_read") is not False:
            errors.append(f"{prefix}.protocol.result_data_read_forbidden")
        if (supersession is None
                and protocol.get("multiple_testing_family_id") != family_id):
            errors.append(f"{prefix}.protocol.multiple_testing_family_id_mismatch")
        if uses_current_lineage:
            effective_lineage = (
                amendment.get("effective_lineage")
                if amendment else protocol.get("lineage")
            )
            if not isinstance(effective_lineage, dict):
                errors.append(f"{prefix}.protocol.lineage_not_object")
            else:
                if effective_lineage != lineage:
                    errors.append(f"{prefix}.protocol.lineage_mismatch")
                if effective_lineage.get(
                    "current_candidate_return_result_read"
                ) is not (status == _BLIND_REJECTED_STATUS):
                    errors.append(f"{prefix}.protocol.current_result_read_mismatch")
                if any(
                    _VERSIONED_CURRENT_RESULT.fullmatch(str(field))
                    for field in effective_lineage
                ):
                    errors.append(
                        f"{prefix}.protocol.versioned_current_result_forbidden"
                    )
            provenance = protocol.get("provenance")
            if not amendment and isinstance(provenance, Mapping) and any(
                _VERSIONED_CURRENT_RESULT.fullmatch(str(field))
                for field in provenance
            ):
                errors.append(
                    f"{prefix}.protocol.versioned_current_result_forbidden"
                )
        elif lineage:
            provenance = protocol.get("provenance") or {}
            if provenance.get("prior_trial_id") != lineage.get("prior_trial_id"):
                errors.append(f"{prefix}.protocol.prior_trial_id_mismatch")
            if provenance.get("prior_trial_outcome_known") is not True:
                errors.append(
                    f"{prefix}.protocol.prior_trial_outcome_known_not_true"
                )
            if provenance.get("current_v2_return_result_read") is not False:
                errors.append(f"{prefix}.protocol.current_result_must_be_unread")
            if protocol.get("status") == "frozen_before_implementation":
                errors.append(
                    f"{prefix}.protocol.outcome_aware_status_misclassified"
                )
        if implementation_frozen:
            implementation = candidate.get("implementation") or {}
            implementation_protocol = (
                amendment.get("effective_implementation_spec")
                if amendment else None
            ) or (
                protocol.get("implementation_spec")
            ) or {}
            protocol_code = (
                implementation_protocol.get("code_identity")
                or {}
            )
            expected_protocol_code = _implementation_protocol_identity(
                implementation
            )
            if isinstance(implementation.get("files"), list):
                protocol_matches = (
                    protocol_code.get("files")
                    == expected_protocol_code.get("files")
                    and isinstance(protocol_code.get("hash_binding"), str)
                    and bool(protocol_code.get("hash_binding"))
                )
            else:
                protocol_matches = protocol_code == expected_protocol_code
            if not protocol_matches:
                errors.append(f"{prefix}.protocol.implementation_identity_mismatch")
            if amendment:
                frozen_tests = amendment.get("effective_frozen_tests")
                if not isinstance(frozen_tests, dict):
                    errors.append(
                        f"{prefix}.protocol_amendment.effective_frozen_tests_invalid"
                    )
                    frozen_tests = {}
            else:
                frozen_tests = protocol.get("frozen_tests") or {}
            if frozen_tests.get("implementation_allowed") is not True:
                errors.append(
                    f"{prefix}.protocol.implementation_allowed_not_true"
                )
            backtest_allowed = frozen_tests.get(
                "backtest_allowed",
                frozen_tests.get("backtest_allowed_after_admission"),
            )
            if runnable is True and backtest_allowed is not True:
                errors.append(f"{prefix}.protocol.backtest_allowed_not_true")
            if status == "data_pending" and backtest_allowed is not False:
                errors.append(
                    f"{prefix}.protocol.backtest_allowed_must_be_false"
                )
            if (status == "production_eligible"
                    and frozen_tests.get("production_allowed") is not True):
                errors.append(f"{prefix}.protocol.production_allowed_not_true")
        elif status == "implementation_pending":
            if candidate.get("implementation") is not None:
                errors.append(f"{prefix}.implementation_forbidden_while_pending")
            if formula.get("local_implementation") is not None:
                errors.append(
                    f"{prefix}.formula.local_implementation_forbidden_while_pending"
                )
            if protocol.get("implementation_exists") is not False:
                errors.append(
                    f"{prefix}.protocol.implementation_exists_must_be_false"
                )
            if protocol.get("frozen_before_implementation") is not True:
                errors.append(
                    f"{prefix}.protocol.frozen_before_implementation_not_true"
                )
            base_frozen_tests = protocol.get("frozen_tests")
            if not isinstance(base_frozen_tests, dict):
                errors.append(f"{prefix}.protocol.frozen_tests_not_object")
                base_frozen_tests = {}
            if base_frozen_tests.get("implementation_allowed") is not False:
                errors.append(
                    f"{prefix}.protocol.implementation_allowed_must_be_false"
                )
            base_backtest_allowed = base_frozen_tests.get(
                "backtest_allowed",
                base_frozen_tests.get("backtest_allowed_after_admission"),
            )
            if base_backtest_allowed is not False:
                errors.append(
                    f"{prefix}.protocol.backtest_allowed_must_be_false"
                )
            if base_frozen_tests.get("production_allowed") is not False:
                errors.append(
                    f"{prefix}.protocol.production_allowed_must_be_false"
                )
            prebuild_state = amendment.get("prebuild_admission_state") or {}
            implementation_authorized = (
                amendment
                and isinstance(prebuild_state, dict)
                and prebuild_state.get("implementation_authorized") is True
            )
            effective_frozen_tests = (
                amendment.get("effective_frozen_tests")
                if implementation_authorized else base_frozen_tests
            )
            if not isinstance(effective_frozen_tests, dict):
                errors.append(
                    f"{prefix}.protocol_amendment.effective_frozen_tests_invalid"
                )
                effective_frozen_tests = {}
            if effective_frozen_tests.get("implementation_allowed") is not (
                True if implementation_authorized else False
            ):
                errors.append(
                    f"{prefix}.protocol.implementation_allowed_mismatch"
                )
            backtest_allowed = effective_frozen_tests.get(
                "backtest_allowed",
                effective_frozen_tests.get("backtest_allowed_after_admission"),
            )
            if backtest_allowed is not False:
                errors.append(
                    f"{prefix}.protocol.backtest_allowed_must_be_false"
                )
            if effective_frozen_tests.get("production_allowed") is not False:
                errors.append(
                    f"{prefix}.protocol.production_allowed_must_be_false"
                )
            for field in ("candidate_signal_computed", "candidate_outcome_computed"):
                if protocol.get(field) is not False:
                    errors.append(f"{prefix}.protocol.{field}_must_be_false")
            if implementation_authorized:
                implementation_spec = amendment.get(
                    "effective_implementation_spec"
                )
                if not isinstance(implementation_spec, dict):
                    errors.append(
                        f"{prefix}.protocol_amendment.implementation_spec_invalid"
                    )
                else:
                    if implementation_spec.get(
                        "implementation_state"
                    ) != "admitted_but_not_started":
                        errors.append(
                            f"{prefix}.protocol_amendment.implementation_state_invalid"
                        )
                    target_files = implementation_spec.get("target_files")
                    if (
                        not isinstance(target_files, list)
                        or not target_files
                        or len(target_files) != len(set(target_files))
                        or any(not _safe_relative_path(path) for path in target_files)
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.target_files_invalid"
                        )
                    test_files = implementation_spec.get("test_files")
                    if (
                        not isinstance(test_files, list)
                        or not test_files
                        or len(test_files) != len(set(test_files))
                        or any(not _safe_relative_path(path) for path in test_files)
                        or set(test_files) & set(target_files or [])
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.test_files_invalid"
                        )
                    code_identity = implementation_spec.get("code_identity")
                    code_identity_object = (
                        code_identity if isinstance(code_identity, dict) else {}
                    )
                    code_files = (
                        code_identity.get("files")
                        if isinstance(code_identity, dict) else None
                    )
                    if (
                        not isinstance(code_files, list)
                        or [item.get("path") for item in code_files
                            if isinstance(item, dict)] != target_files
                        or len(code_files) != len(target_files or [])
                        or not isinstance(
                            code_identity_object.get("hash_binding"), str
                        )
                        or not code_identity_object.get(
                            "hash_binding", ""
                        ).strip()
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.code_identity_invalid"
                        )
                    else:
                        for index, item in enumerate(code_files):
                            scope = item.get("scope")
                            if (
                                not isinstance(scope, list)
                                or not scope
                                or any(
                                    not isinstance(value, str)
                                    or not value.strip()
                                    for value in scope
                                )
                            ):
                                errors.append(
                                    f"{prefix}.protocol_amendment.code_identity.files[{index}].scope_invalid"
                                )
                    expected_target_contract = _prebuild_target_contract(
                        str(candidate_id), protocol,
                    )
                    if implementation_spec.get(
                        "target_contract"
                    ) != expected_target_contract:
                        errors.append(
                            f"{prefix}.protocol_amendment.target_contract_mismatch"
                        )
                    expected_permissions = {
                        "future_data_allowed": False,
                        "generic_panel_allowed": False,
                        "real_panel_allowed": False,
                        "mining_allowed": False,
                        "backtest_allowed": False,
                        "production_allowed": False,
                    }
                    if implementation_spec.get(
                        "execution_permissions"
                    ) != expected_permissions:
                        errors.append(
                            f"{prefix}.protocol_amendment.execution_permissions_invalid"
                        )
                    if (protocol.get("formula") or {}).get("kind") in {
                        "characteristic", "ratio_characteristic",
                    }:
                        _verify_prebuild_recipe_contract(
                            str(candidate_id), protocol, implementation_spec,
                            f"{prefix}.protocol_amendment", errors,
                        )
        if input_feasibility is not None:
            effective_feasibility = (
                amendment.get("effective_input_feasibility")
                if amendment else protocol.get("input_feasibility")
            )
            if effective_feasibility != input_feasibility:
                errors.append(f"{prefix}.protocol.input_feasibility_mismatch")
    if amendment:
        base_protocol = amendment.get("base_protocol") or {}
        trigger = amendment.get("trigger") or {}
        if amendment.get("schema_version") != 1:
            errors.append(f"{prefix}.protocol_amendment.schema_version_invalid")
        amendment_number = _protocol_amendment_number(
            candidate_id, amendment.get("amendment_id")
        )
        if amendment_number is None:
            errors.append(f"{prefix}.protocol_amendment.amendment_id_mismatch")
        prebuild_state = amendment.get("prebuild_admission_state")
        sealed_state = amendment.get("sealed_implementation_state")
        postbuild_state = amendment.get("postbuild_admission_state")
        blind_rejection_state = amendment.get("blind_rejection_state")
        phase_states = (
            prebuild_state, sealed_state, postbuild_state,
            blind_rejection_state,
        )
        if sum(state is not None for state in phase_states) > 1:
            errors.append(
                f"{prefix}.protocol_amendment.phase_states_mutually_exclusive"
            )
        if supersession is None and status == "implementation_pending" and (
            prebuild_state is None
        ):
            errors.append(
                f"{prefix}.protocol_amendment.prebuild_state_required"
            )
        if prebuild_state is not None:
            if status != "implementation_pending" or runnable is not False:
                errors.append(
                    f"{prefix}.protocol_amendment.prebuild_phase_locked"
                )
            if candidate.get("implementation") is not None:
                errors.append(
                    f"{prefix}.protocol_amendment.prebuild_implementation_forbidden"
                )
            if formula.get("local_implementation") is not None:
                errors.append(
                    f"{prefix}.protocol_amendment.prebuild_local_implementation_forbidden"
                )
        if status == _SEALED_IMPLEMENTATION_STATUS and sealed_state is None:
            errors.append(
                f"{prefix}.protocol_amendment.sealed_state_required"
            )
        if sealed_state is not None and (
            status != _SEALED_IMPLEMENTATION_STATUS or runnable is not False
        ):
            errors.append(f"{prefix}.protocol_amendment.sealed_phase_locked")
        if status == _BLIND_REJECTED_STATUS and blind_rejection_state is None:
            errors.append(
                f"{prefix}.protocol_amendment.blind_rejection_state_required"
            )
        if blind_rejection_state is not None and (
            status != _BLIND_REJECTED_STATUS or runnable is not False
        ):
            errors.append(
                f"{prefix}.protocol_amendment.blind_rejection_phase_locked"
            )
        if runnable is True and postbuild_state is None:
            errors.append(
                f"{prefix}.protocol_amendment.postbuild_state_required"
            )
        if postbuild_state is not None and (
            status != "implementation_ready" or runnable is not True
        ):
            errors.append(f"{prefix}.protocol_amendment.postbuild_phase_locked")
        if amendment.get("protocol_id") != candidate_id:
            errors.append(f"{prefix}.protocol_amendment.candidate_id_mismatch")
        if amendment.get("status") != preregistration.get("amendment_status"):
            errors.append(f"{prefix}.protocol_amendment.status_mismatch")
        if blind_rejection_state is not None:
            for field in (
                "result_data_read", "current_candidate_return_result_read",
                "candidate_signal_computed", "candidate_rank_computed",
                "candidate_outcome_computed", "ic_or_gate_computed",
            ):
                if amendment.get(field) is not True:
                    errors.append(
                        f"{prefix}.protocol_amendment.{field}_must_be_true"
                    )
            for field in (
                "price_delay_computed", "mining_or_backtest_run",
                "paid_api_used",
            ):
                if amendment.get(field) is not False:
                    errors.append(
                        f"{prefix}.protocol_amendment.{field}_must_be_false"
                    )
        else:
            if amendment.get("result_data_read") is not False:
                errors.append(
                    f"{prefix}.protocol_amendment.result_data_read_forbidden"
                )
            if amendment.get("candidate_outcome_computed") is not False:
                errors.append(
                    f"{prefix}.protocol_amendment.outcome_computed_forbidden"
                )
        if any(state is not None for state in phase_states) and (
            blind_rejection_state is None
        ):
            for field in (
                "candidate_signal_computed",
                "price_delay_computed",
                "ic_or_gate_computed",
                "mining_or_backtest_run",
                "paid_api_used",
            ):
                if amendment.get(field) is not False:
                    errors.append(
                        f"{prefix}.protocol_amendment.{field}_must_be_false"
                    )
            if amendment.get("current_candidate_return_result_read") is not False:
                errors.append(
                    f"{prefix}.protocol_amendment.current_candidate_return_result_read_must_be_false"
                )
        if prebuild_state is not None:
            if amendment.get("implementation_exists") is not False:
                errors.append(
                    f"{prefix}.protocol_amendment.implementation_exists_must_be_false"
                )
            if amendment.get("frozen_before_implementation") is not True:
                errors.append(
                    f"{prefix}.protocol_amendment.frozen_before_implementation_not_true"
                )
        if (
            sealed_state is not None
            or postbuild_state is not None
            or blind_rejection_state is not None
        ) and amendment.get(
            "implementation_exists"
        ) is not True:
            errors.append(
                f"{prefix}.protocol_amendment.implementation_exists_not_true"
            )
        if trigger.get("outcome_information_used") is not (
            blind_rejection_state is not None
        ):
            errors.append(
                f"{prefix}.protocol_amendment.outcome_information_mismatch"
            )
        expected_base = {
            "path": preregistration.get("protocol_path"),
            "sha256": preregistration.get("protocol_sha256"),
            "status": preregistration.get("protocol_status"),
        }
        if base_protocol != expected_base:
            errors.append(f"{prefix}.protocol_amendment.base_protocol_mismatch")
        if uses_current_lineage and amendment.get("effective_lineage") != lineage:
            errors.append(f"{prefix}.protocol_amendment.lineage_mismatch")
        if (input_feasibility is not None
                and amendment.get("effective_input_feasibility")
                != input_feasibility):
            errors.append(
                f"{prefix}.protocol_amendment.input_feasibility_mismatch"
            )
        bindings = amendment.get("input_identity_bindings")
        required_bindings = {
            "canonical_panel",
            "panel_manifest",
            "current_source_manifest",
            "stable_instrument_aliases",
            "security_master",
            "company_action_manifest",
            "v4_daily_download_manifest",
            "v4_daily_quality_audit",
            "official_trading_calendar",
        }
        binding_artifacts: dict[str, dict[str, Any]] = {}
        if (
            (
                amendment.get("prebuild_admission_state") is not None
                or amendment.get("sealed_implementation_state") is not None
                or amendment.get("postbuild_admission_state") is not None
                or amendment.get("blind_rejection_state") is not None
            )
            and bindings is None
        ):
            errors.append(
                f"{prefix}.protocol_amendment.input_bindings_required"
            )
        v4_bindings = (
            isinstance(bindings, dict) and set(bindings) == required_bindings
        )
        if bindings is not None and not v4_bindings:
            active_feasibility = feasibility
            if sealed_state is not None or blind_rejection_state is not None:
                active_feasibility = deepcopy(feasibility)
                active_feasibility["input_bindings"] = deepcopy(bindings)
            _verify_generic_input_bindings(
                root,
                bindings,
                active_feasibility,
                f"{prefix}.protocol_amendment.input_bindings",
                errors,
            )
        if v4_bindings:
            for name in required_bindings - {"canonical_panel"}:
                binding = bindings.get(name) or {}
                label = f"{prefix}.protocol_amendment.input_bindings.{name}"
                if name == "stable_instrument_aliases":
                    _bound_path, alias_bytes = _verified_local_file(
                        root, binding.get("path"), binding.get("sha256"),
                        label, errors, keep_bytes=True,
                    )
                    official_sources = _verified_official_sources(
                        root,
                        alias_bytes,
                        f"{label}.official_sources",
                        errors,
                    )
                    if amendment.get("official_source_evidence") != (
                        official_sources
                    ):
                        errors.append(
                            f"{label}.official_source_evidence_mismatch"
                        )
                elif name == "security_master":
                    _local_file(
                        root, binding.get("path"), binding.get("sha256"),
                        label, errors,
                    )
                else:
                    binding_artifacts[name] = _local_artifact(
                        root, binding.get("path"), binding.get("sha256"),
                        label, errors,
                    )
            canonical = bindings.get("canonical_panel") or {}
            canonical_sha = canonical.get("sha256")
            _local_file(
                root,
                canonical.get("path"),
                canonical_sha,
                f"{prefix}.protocol_amendment.input_bindings.canonical_panel",
                errors,
            )
            panel_manifest = binding_artifacts.get("panel_manifest") or {}
            if (panel_manifest.get("outputs") or {}).get(
                "universe_daily.parquet"
            ) != canonical_sha:
                errors.append(
                    f"{prefix}.protocol_amendment.input_bindings.panel_sha_mismatch"
                )
            recorded_source = (panel_manifest.get("source_manifest") or {}).get(
                "sha256"
            )
            if recorded_source != (bindings.get("panel_manifest") or {}).get(
                "recorded_source_manifest_sha256"
            ):
                errors.append(
                    f"{prefix}.protocol_amendment.input_bindings.recorded_source_sha_mismatch"
                )
            source_manifest = binding_artifacts.get("current_source_manifest") or {}
            source_snapshot = (bindings.get("current_source_manifest") or {}).get(
                "canonical_source_snapshot_sha256"
            )
            if source_manifest.get("source_snapshot_sha256") != source_snapshot:
                errors.append(
                    f"{prefix}.protocol_amendment.input_bindings.source_snapshot_mismatch"
                )
            if input_integrity:
                if input_integrity.get("panel_sha256") != canonical_sha:
                    errors.append(
                        f"{prefix}.protocol_amendment.input_bindings.audit_panel_sha_mismatch"
                    )
                if input_integrity.get("source_snapshot_sha256") != source_snapshot:
                    errors.append(
                        f"{prefix}.protocol_amendment.input_bindings.audit_source_snapshot_mismatch"
                    )
                company_sha = (
                    input_integrity.get("company_action_evidence") or {}
                ).get("sha256")
                if company_sha != (bindings.get("company_action_manifest") or {}).get(
                    "sha256"
                ):
                    errors.append(
                        f"{prefix}.protocol_amendment.input_bindings.company_action_sha_mismatch"
                    )
        prior_amendment_binding = amendment.get("prior_amendment")
        prior_amendment: dict[str, Any] = {}
        if prior_amendment_binding is not None:
            if not isinstance(prior_amendment_binding, dict):
                errors.append(
                    f"{prefix}.protocol_amendment.prior_amendment_not_object"
                )
            else:
                prior_amendment = _local_artifact(
                    root,
                    prior_amendment_binding.get("path"),
                    prior_amendment_binding.get("sha256"),
                    f"{prefix}.protocol_amendment.prior_amendment",
                    errors,
                )
                if prior_amendment:
                    if prior_amendment.get("protocol_id") != candidate_id:
                        errors.append(
                            f"{prefix}.protocol_amendment.prior_amendment_candidate_mismatch"
                        )
                    if prior_amendment.get("status") != prior_amendment_binding.get(
                        "status"
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.prior_amendment_status_mismatch"
                        )
                    if prior_amendment.get("result_data_read") is not False:
                        errors.append(
                            f"{prefix}.protocol_amendment.prior_amendment_result_forbidden"
                        )
                    if prior_amendment.get("candidate_outcome_computed") is not False:
                        errors.append(
                            f"{prefix}.protocol_amendment.prior_amendment_outcome_forbidden"
                        )
                    prior_number = _protocol_amendment_number(
                        candidate_id, prior_amendment.get("amendment_id")
                    )
                    if (
                        prior_number is None
                        or amendment_number != prior_number + 1
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.amendment_sequence_invalid"
                        )
        elif amendment_number not in {None, 1}:
            errors.append(
                f"{prefix}.protocol_amendment.amendment_sequence_invalid"
            )
        prebuild_state = amendment.get("prebuild_admission_state")
        sealed_state = amendment.get("sealed_implementation_state")
        if (
            prebuild_state is not None
            or sealed_state is not None
            or blind_rejection_state is not None
        ):
            evaluator_binding = amendment.get("evaluator_contract")
            if not isinstance(evaluator_binding, dict):
                errors.append(
                    f"{prefix}.protocol_amendment.evaluator_contract_not_object"
                )
            else:
                if evaluator_binding.get("research_epoch") != config.RESEARCH_EPOCH:
                    errors.append(
                        f"{prefix}.protocol_amendment.research_epoch_mismatch"
                    )
                if (
                    evaluator_binding.get("l0_contract_version")
                    != config.L0_CONTRACT_VERSION
                ):
                    errors.append(
                        f"{prefix}.protocol_amendment.l0_contract_version_mismatch"
                    )
                evaluator_contract = _local_artifact(
                    root,
                    evaluator_binding.get("path"),
                    evaluator_binding.get("sha256"),
                    f"{prefix}.protocol_amendment.evaluator_contract",
                    errors,
                )
                if evaluator_contract:
                    if (
                        evaluator_contract.get("contract_id")
                        != evaluator_binding.get("l0_contract_version")
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.evaluator_contract_id_mismatch"
                        )
                    if (
                        evaluator_contract.get("research_epoch")
                        != evaluator_binding.get("research_epoch")
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.evaluator_contract_epoch_mismatch"
                        )

        prior_admission_binding = amendment.get("prior_admission")
        prior_admission = {}
        if prior_admission_binding is not None:
            if not isinstance(prior_admission_binding, dict):
                errors.append(
                    f"{prefix}.protocol_amendment.prior_admission_not_object"
                )
            else:
                prior_admission = _local_artifact(
                    root,
                    prior_admission_binding.get("path"),
                    prior_admission_binding.get("sha256"),
                    f"{prefix}.protocol_amendment.prior_admission",
                    errors,
                )
                if prior_admission:
                    if prior_admission.get("candidate_id") != candidate_id:
                        errors.append(
                            f"{prefix}.protocol_amendment.prior_admission_candidate_mismatch"
                        )
                    if prior_admission.get("status") != prior_admission_binding.get(
                        "status"
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.prior_admission_status_mismatch"
                        )
                    if prior_admission.get("result_data_read") is not False:
                        errors.append(
                            f"{prefix}.protocol_amendment.prior_admission_result_forbidden"
                        )
                    sealed_prior = (
                        candidate.get("status") in {
                            _SEALED_IMPLEMENTATION_STATUS,
                            _BLIND_REJECTED_STATUS,
                        }
                        and prior_admission.get("status")
                        == _SEALED_IMPLEMENTATION_STATUS
                        and prior_admission.get("implementation_built") is True
                        and prior_admission.get("implementation_admitted") is True
                        and prior_admission.get("runnable") is False
                        and prior_admission.get("result_data_read") is False
                    )
                    if (
                        prior_admission.get("implementation_admitted") is not False
                        and not sealed_prior
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.prior_admission_implementation_must_be_false"
                        )
        if supersession is None and amendment_number not in {None, 1}:
            if not prior_amendment:
                errors.append(
                    f"{prefix}.protocol_amendment.prior_amendment_required"
                )
            if not prior_admission:
                errors.append(
                    f"{prefix}.protocol_amendment.prior_admission_required"
                )
        if (
            amendment_number == 2
            and prior_amendment
            and prior_admission
        ):
            direct_prior_identity = {
                "protocol_amendment_path": prior_amendment_binding.get("path"),
                "protocol_amendment_sha256": prior_amendment_binding.get("sha256"),
                "protocol_amendment_status": prior_amendment_binding.get("status"),
            }
            if any(
                prior_admission.get(field) != value
                for field, value in direct_prior_identity.items()
            ):
                errors.append(
                    f"{prefix}.protocol_amendment.prior_admission_amendment_binding_mismatch"
                )
        if (
            amendment_number is not None
            and amendment_number >= 3
            and prior_amendment
            and prior_admission
        ):
            direct_prior_identity = {
                "protocol_amendment_path": prior_amendment_binding.get("path"),
                "protocol_amendment_sha256": prior_amendment_binding.get("sha256"),
                "protocol_amendment_status": prior_amendment_binding.get("status"),
            }
            if any(
                prior_admission.get(field) != value
                for field, value in direct_prior_identity.items()
            ):
                errors.append(
                    f"{prefix}.protocol_amendment.prior_admission_amendment_binding_mismatch"
                )

            historical_amendment_binding = prior_amendment.get(
                "prior_amendment"
            )
            historical_admission_binding = prior_amendment.get(
                "prior_admission"
            )
            if not isinstance(historical_amendment_binding, dict) or not isinstance(
                historical_admission_binding, dict
            ):
                errors.append(
                    f"{prefix}.protocol_amendment.historical_chain_missing"
                )
            else:
                historical_amendment = _local_artifact(
                    root,
                    historical_amendment_binding.get("path"),
                    historical_amendment_binding.get("sha256"),
                    f"{prefix}.protocol_amendment.historical_amendment",
                    errors,
                )
                historical_admission = _local_artifact(
                    root,
                    historical_admission_binding.get("path"),
                    historical_admission_binding.get("sha256"),
                    f"{prefix}.protocol_amendment.historical_admission",
                    errors,
                )
                if historical_amendment and historical_admission:
                    recorded_path = historical_admission.get(
                        "protocol_amendment_path"
                    )
                    recorded_sha = historical_admission.get(
                        "protocol_amendment_sha256"
                    )
                    history_is_discontinuous = (
                        recorded_path != historical_amendment_binding.get("path")
                        or recorded_sha
                        != historical_amendment_binding.get("sha256")
                    )
                    discontinuity = amendment.get(
                        "historical_chain_discontinuity"
                    )
                    if history_is_discontinuous:
                        incident_binding = (
                            discontinuity.get("incident_manifest")
                            if isinstance(discontinuity, dict) else None
                        )
                        incident = _local_artifact(
                            root,
                            (incident_binding or {}).get("path"),
                            (incident_binding or {}).get("sha256"),
                            f"{prefix}.protocol_amendment.chain_incident",
                            errors,
                        )
                        expected_discontinuity = {
                            "status": "acknowledged_unrepaired_append_only",
                            "incident_manifest": incident_binding,
                            "amendment_path": historical_amendment_binding.get(
                                "path"
                            ),
                            "amendment_sha256": historical_amendment_binding.get(
                                "sha256"
                            ),
                            "admission_path": historical_admission_binding.get(
                                "path"
                            ),
                            "admission_sha256": historical_admission_binding.get(
                                "sha256"
                            ),
                            "admission_recorded_amendment_path": recorded_path,
                            "admission_recorded_amendment_sha256": recorded_sha,
                            "amendment_2_path": prior_amendment_binding.get("path"),
                            "amendment_2_sha256": prior_amendment_binding.get(
                                "sha256"
                            ),
                            "admission_2_path": prior_admission_binding.get("path"),
                            "admission_2_sha256": prior_admission_binding.get(
                                "sha256"
                            ),
                            "outcome_information_used": False,
                            "repair_policy": (
                                "append_only_successor_without_rewriting_"
                                "historical_bytes"
                            ),
                        }
                        if discontinuity != expected_discontinuity:
                            errors.append(
                                f"{prefix}.protocol_amendment.chain_discontinuity_mismatch"
                            )
                        if incident:
                            if (
                                incident.get("append_only") is not True
                                or incident.get("content_addressed_filenames")
                                is not True
                            ):
                                errors.append(
                                    f"{prefix}.protocol_amendment.chain_incident_contract_invalid"
                                )
                            expected_history = {
                                historical_amendment_binding.get("path"):
                                historical_amendment_binding.get("sha256"),
                                historical_admission_binding.get("path"):
                                historical_admission_binding.get("sha256"),
                                prior_amendment_binding.get("path"):
                                prior_amendment_binding.get("sha256"),
                                prior_admission_binding.get("path"):
                                prior_admission_binding.get("sha256"),
                            }
                            manifest_history: dict[str, str] = {}
                            for index, item in enumerate(
                                incident.get("artifacts") or []
                            ):
                                if not isinstance(item, dict):
                                    errors.append(
                                        f"{prefix}.protocol_amendment.chain_incident_artifact_invalid"
                                    )
                                    continue
                                archived = _local_file(
                                    root,
                                    item.get("archive_path"),
                                    item.get("observed_sha256"),
                                    f"{prefix}.protocol_amendment.chain_incident_artifacts[{index}]",
                                    errors,
                                )
                                if (
                                    archived is not None
                                    and archived.stat().st_size
                                    != item.get("observed_size_bytes")
                                ):
                                    errors.append(
                                        f"{prefix}.protocol_amendment.chain_incident_artifacts[{index}]_size_mismatch"
                                    )
                                manifest_history[item.get("source_path")] = item.get(
                                    "first_created_sha256"
                                )
                            if manifest_history != expected_history:
                                errors.append(
                                    f"{prefix}.protocol_amendment.chain_incident_history_mismatch"
                                )
                    elif discontinuity is not None:
                        errors.append(
                            f"{prefix}.protocol_amendment.chain_discontinuity_unexpected"
                        )
        if prebuild_state is not None:
            if not isinstance(prebuild_state, dict):
                errors.append(
                    f"{prefix}.protocol_amendment.prebuild_state_not_object"
                )
            else:
                expected_prebuild = {
                    "candidate_status": "implementation_pending",
                    "implementation_authorized": True,
                    "implementation_built": False,
                    "implementation_admitted": False,
                    "runnable": False,
                    "backtest_admitted": False,
                    "production_admitted": False,
                }
                if prebuild_state != expected_prebuild:
                    errors.append(
                        f"{prefix}.protocol_amendment.prebuild_state_mismatch"
                    )
                if not prior_admission:
                    errors.append(
                        f"{prefix}.protocol_amendment.prior_admission_required"
                    )
            gate_change_scope = amendment.get("admission_gate_change_scope")
            expected_gate_paths = [
                "factor_system/source_gate.py",
                "factor_system/tests/test_source_gate.py",
            ]
            if (
                not isinstance(gate_change_scope, dict)
                or gate_change_scope.get("target_files") != expected_gate_paths
                or not isinstance(gate_change_scope.get("contract"), str)
                or not gate_change_scope.get("contract", "").strip()
            ):
                errors.append(
                    f"{prefix}.protocol_amendment.gate_change_scope_invalid"
                )
            authorization_gate = amendment.get("authorization_gate")
            gate_files = (
                authorization_gate.get("files")
                if isinstance(authorization_gate, dict) else None
            )
            if (
                not isinstance(gate_files, list)
                or [item.get("path") for item in gate_files
                    if isinstance(item, dict)] != expected_gate_paths
                or len(gate_files) != len(expected_gate_paths)
            ):
                errors.append(
                    f"{prefix}.protocol_amendment.authorization_gate_invalid"
                )
            else:
                for index, item in enumerate(gate_files):
                    _local_file(
                        root,
                        item.get("path"),
                        item.get("sha256"),
                        f"{prefix}.protocol_amendment.authorization_gate.files[{index}]",
                        errors,
                    )
                    scope = item.get("scope")
                    if (
                        not isinstance(scope, list)
                        or not scope
                        or any(not isinstance(value, str) or not value.strip()
                               for value in scope)
                    ):
                        errors.append(
                            f"{prefix}.protocol_amendment.authorization_gate.files[{index}].scope_invalid"
                        )
                verification = authorization_gate.get("verification") or {}
                if (
                    verification.get("kind") != "static_py_compile"
                    or verification.get("status") != "passed"
                    or verification.get("command")
                    != _PREBUILD_AUTHORIZATION_GATE_COMMAND
                    or verification.get("checked_files") != len(gate_files)
                    or verification.get("failed") != 0
                    or verification.get("pytest_run") is not False
                ):
                    errors.append(
                        f"{prefix}.protocol_amendment.authorization_gate.verification_invalid"
                    )
        if sealed_state is not None:
            if not isinstance(sealed_state, dict):
                errors.append(
                    f"{prefix}.protocol_amendment.sealed_state_not_object"
                )
            elif _canonical_json(sealed_state) != _canonical_json(
                _SEALED_IMPLEMENTATION_STATE
            ):
                errors.append(
                    f"{prefix}.protocol_amendment.sealed_state_mismatch"
                )
            if amendment_number in {None, 1}:
                errors.append(
                    f"{prefix}.protocol_amendment.sealed_successor_required"
                )
            if not prior_amendment or not prior_admission:
                errors.append(
                    f"{prefix}.protocol_amendment.sealed_direct_prior_required"
                )
            implementation_spec = amendment.get("effective_implementation_spec")
            if not isinstance(implementation_spec, dict):
                errors.append(
                    f"{prefix}.protocol_amendment.implementation_spec_invalid"
                )
            elif implementation_spec.get(
                "implementation_state"
            ) != "built_and_synthetic_verified":
                errors.append(
                    f"{prefix}.protocol_amendment.implementation_state_invalid"
                )
            if prior_amendment and prior_admission:
                _verify_sealed_implementation(
                    root,
                    candidate,
                    protocol,
                    amendment,
                    admission,
                    prior_amendment,
                    prior_admission,
                    prefix,
                    errors,
                )
        if blind_rejection_state is not None:
            if blind_rejection_state != _BLIND_REJECTION_STATE:
                errors.append(
                    f"{prefix}.protocol_amendment.blind_rejection_state_mismatch"
                )
            if amendment_number in {None, 1}:
                errors.append(
                    f"{prefix}.protocol_amendment.blind_rejection_successor_required"
                )
            if not prior_amendment or not prior_admission:
                errors.append(
                    f"{prefix}.protocol_amendment.blind_rejection_direct_prior_required"
                )
            else:
                _verify_blind_rejection(
                    root,
                    candidate,
                    amendment,
                    admission,
                    prior_amendment,
                    prior_admission,
                    prefix,
                    errors,
                )
        if postbuild_state is not None:
            expected_postbuild = {
                "candidate_status": "implementation_ready",
                "implementation_authorized": True,
                "implementation_built": True,
                "implementation_admitted": True,
                "runnable": True,
                "backtest_admitted": True,
                "production_admitted": False,
            }
            if not isinstance(postbuild_state, dict):
                errors.append(
                    f"{prefix}.protocol_amendment.postbuild_state_not_object"
                )
            elif postbuild_state != expected_postbuild:
                errors.append(
                    f"{prefix}.protocol_amendment.postbuild_state_mismatch"
                )
            if amendment_number in {None, 1}:
                errors.append(
                    f"{prefix}.protocol_amendment.postbuild_successor_required"
                )
            if not prior_amendment or not prior_admission:
                errors.append(
                    f"{prefix}.protocol_amendment.postbuild_direct_prior_required"
                )
            implementation_spec = amendment.get("effective_implementation_spec")
            if not isinstance(implementation_spec, dict):
                errors.append(
                    f"{prefix}.protocol_amendment.implementation_spec_invalid"
                )
            else:
                if implementation_spec.get(
                    "implementation_state"
                ) != "built_and_synthetic_verified":
                    errors.append(
                        f"{prefix}.protocol_amendment.implementation_state_invalid"
                    )
                if implementation_spec.get(
                    "target_contract"
                ) != _prebuild_target_contract(str(candidate_id), protocol):
                    errors.append(
                        f"{prefix}.protocol_amendment.target_contract_mismatch"
                    )
                expected_permissions = {
                    "future_data_allowed": False,
                    "generic_panel_allowed": True,
                    "real_panel_allowed": True,
                    "mining_allowed": True,
                    "backtest_allowed": True,
                    "production_allowed": False,
                }
                if implementation_spec.get(
                    "execution_permissions"
                ) != expected_permissions:
                    errors.append(
                        f"{prefix}.protocol_amendment.execution_permissions_invalid"
                    )
    if admission:
        if admission.get("candidate_id") != candidate_id:
            errors.append(f"{prefix}.admission.candidate_id_mismatch")
        if admission.get("status") != preregistration.get("admission_status"):
            errors.append(f"{prefix}.admission.status_mismatch")
        if admission.get("status") != status:
            errors.append(f"{prefix}.admission.candidate_status_mismatch")
        if admission.get("runnable") is not runnable:
            errors.append(f"{prefix}.admission.runnable_mismatch")
        if admission.get("result_data_read") is not (
            status == _BLIND_REJECTED_STATUS
        ):
            errors.append(f"{prefix}.admission.result_data_read_mismatch")
        if admission.get("paid_api_required") is not False:
            errors.append(f"{prefix}.admission.paid_api_forbidden")
        if (supersession is None
                and admission.get("multiple_testing_family_id") != family_id):
            errors.append(f"{prefix}.admission.multiple_testing_family_id_mismatch")
        if admission.get("protocol_sha256") != preregistration.get(
            "protocol_sha256"
        ):
            errors.append(f"{prefix}.admission.protocol_sha256_mismatch")
        if amendment:
            if admission.get("protocol_amendment_path") != preregistration.get(
                "amendment_path"
            ):
                errors.append(
                    f"{prefix}.admission.protocol_amendment_path_mismatch"
                )
            if admission.get("protocol_amendment_sha256") != preregistration.get(
                "amendment_sha256"
            ):
                errors.append(
                    f"{prefix}.admission.protocol_amendment_sha256_mismatch"
                )
            if (
                admission.get("protocol_amendment_status") is not None
                and admission.get("protocol_amendment_status")
                != preregistration.get("amendment_status")
            ):
                errors.append(
                    f"{prefix}.admission.protocol_amendment_status_mismatch"
                )
            prior_binding = amendment.get("prior_admission") or {}
            if admission.get("prior_admission_path") != prior_binding.get("path"):
                errors.append(
                    f"{prefix}.admission.prior_admission_path_mismatch"
                )
            if admission.get("prior_admission_sha256") != prior_binding.get(
                "sha256"
            ):
                errors.append(
                    f"{prefix}.admission.prior_admission_sha256_mismatch"
                )
            evaluator_binding = amendment.get("evaluator_contract") or {}
            evaluator_fields = {
                "research_epoch": "research_epoch",
                "l0_contract_version": "l0_contract_version",
                "evaluator_contract_path": "path",
                "evaluator_contract_sha256": "sha256",
            }
            for admission_field, amendment_field in evaluator_fields.items():
                if admission.get(admission_field) != evaluator_binding.get(
                    amendment_field
                ):
                    errors.append(
                        f"{prefix}.admission.{admission_field}_mismatch"
                    )
            if amendment.get("prebuild_admission_state") is not None:
                if (
                    admission.get("status") != "implementation_pending"
                    or admission.get("runnable") is not False
                ):
                    errors.append(
                        f"{prefix}.admission.prebuild_phase_locked"
                    )
                for field in (
                    "implementation_built",
                    "implementation_admitted",
                    "backtest_admitted",
                    "production_admitted",
                    "candidate_signal_computed",
                    "candidate_outcome_computed",
                    "price_delay_computed",
                    "ic_or_gate_computed",
                    "mining_or_backtest_run",
                    "paid_api_used",
                ):
                    if admission.get(field) is not False:
                        errors.append(
                            f"{prefix}.admission.prebuild_{field}_must_be_false"
                        )
            if amendment.get("postbuild_admission_state") is not None:
                expected_postbuild_admission = {
                    "status": admission.get("status"),
                    "implementation_authorized": admission.get(
                        "implementation_authorized"
                    ),
                    "implementation_built": admission.get(
                        "implementation_built"
                    ),
                    "implementation_admitted": admission.get(
                        "implementation_admitted"
                    ),
                    "runnable": admission.get("runnable"),
                    "backtest_admitted": admission.get("backtest_admitted"),
                    "production_admitted": admission.get(
                        "production_admitted"
                    ),
                }
                if amendment.get(
                    "postbuild_admission_state"
                ) != expected_postbuild_admission:
                    errors.append(
                        f"{prefix}.admission.postbuild_state_mismatch"
                    )
                for field in (
                    "candidate_signal_computed",
                    "candidate_outcome_computed",
                    "price_delay_computed",
                    "ic_or_gate_computed",
                    "mining_or_backtest_run",
                    "paid_api_used",
                ):
                    if admission.get(field) is not False:
                        errors.append(
                            f"{prefix}.admission.postbuild_{field}_must_be_false"
                        )
            if amendment.get("sealed_implementation_state") is not None:
                expected_sealed_admission = {
                    key: (
                        admission.get("status")
                        if key == "candidate_status" else admission.get(key)
                    ) for key in _SEALED_IMPLEMENTATION_STATE
                }
                if _canonical_json(expected_sealed_admission) != _canonical_json(
                    _SEALED_IMPLEMENTATION_STATE
                ):
                    errors.append(f"{prefix}.admission.sealed_state_mismatch")
                for field in _SEALED_AUDIT_FALSE_FIELDS:
                    if admission.get(field) is not False:
                        errors.append(
                            f"{prefix}.admission.sealed_{field}_must_be_false"
                        )
            if amendment.get("blind_rejection_state") is not None:
                for field in (
                    "candidate_signal_computed", "candidate_rank_computed",
                    "candidate_outcome_computed", "ic_or_gate_computed",
                ):
                    if admission.get(field) is not True:
                        errors.append(
                            f"{prefix}.admission.blind_rejection_{field}_must_be_true"
                        )
                for field in (
                    "price_delay_computed", "mining_or_backtest_run",
                    "paid_api_used",
                ):
                    if admission.get(field) is not False:
                        errors.append(
                            f"{prefix}.admission.blind_rejection_{field}_must_be_false"
                        )
        if input_feasibility is not None:
            if admission.get("input_feasibility_path") != input_feasibility.get(
                "path"
            ):
                errors.append(
                    f"{prefix}.admission.input_feasibility_path_mismatch"
                )
            if admission.get("input_feasibility_sha256") != input_feasibility.get(
                "sha256"
            ):
                errors.append(
                    f"{prefix}.admission.input_feasibility_sha256_mismatch"
                )
        if implementation_frozen:
            implementation = candidate.get("implementation") or {}
            if isinstance(implementation.get("files"), list):
                expected_files = _implementation_audit_identity(
                    implementation
                ).get("files")
                if admission.get("implementation_files") != expected_files:
                    errors.append(
                        f"{prefix}.admission.implementation_files_mismatch"
                    )
            elif admission.get("implementation_sha256") != implementation.get(
                "sha256"
            ):
                errors.append(f"{prefix}.admission.implementation_sha256_mismatch")
            if admission.get("implementation_audit_path") != implementation.get(
                "audit_path"
            ):
                errors.append(
                    f"{prefix}.admission.implementation_audit_path_mismatch"
                )
            if admission.get("implementation_audit_sha256") != implementation.get(
                "audit_sha256"
            ):
                errors.append(
                    f"{prefix}.admission.implementation_audit_sha256_mismatch"
                )
            if admission.get("implementation_admitted") is not True:
                errors.append(
                    f"{prefix}.admission.implementation_admitted_not_true"
                )
            if runnable is True and admission.get("backtest_admitted") is not True:
                errors.append(f"{prefix}.admission.backtest_admitted_not_true")
            checks = admission.get("checks") or {}
            for check in (
                "a_share_benchmark_definition_frozen",
                "a_share_return_mapping_frozen",
                "point_in_time_tests_passed",
            ):
                if checks.get(check) is not True:
                    errors.append(f"{prefix}.admission.{check}_not_true")
            if status == "data_pending":
                if admission.get("backtest_admitted") is not False:
                    errors.append(
                        f"{prefix}.admission.backtest_admitted_must_be_false"
                    )
                if admission.get("production_admitted") is not False:
                    errors.append(
                        f"{prefix}.admission.production_admitted_must_be_false"
                    )
                if checks.get("data_window_feasibility_passed") is not False:
                    errors.append(
                        f"{prefix}.admission.data_window_feasibility_must_be_false"
                    )
            elif status == _SEALED_IMPLEMENTATION_STATUS:
                if admission.get("backtest_admitted") is not False:
                    errors.append(
                        f"{prefix}.admission.backtest_admitted_must_be_false"
                    )
                if admission.get("production_admitted") is not False:
                    errors.append(
                        f"{prefix}.admission.production_admitted_must_be_false"
                    )
                if admission.get("runnable") is not False:
                    errors.append(f"{prefix}.admission.runnable_must_be_false")
                if checks.get("data_window_feasibility_passed") is not True:
                    errors.append(
                        f"{prefix}.admission.data_window_feasibility_not_true"
                    )
            elif status == _BLIND_REJECTED_STATUS:
                for field in (
                    "blind_evaluation_admitted", "backtest_admitted",
                    "production_admitted", "runnable",
                ):
                    if admission.get(field) is not False:
                        errors.append(
                            f"{prefix}.admission.{field}_must_be_false"
                        )
                if admission.get("blind_evaluation_completed") is not True:
                    errors.append(
                        f"{prefix}.admission.blind_evaluation_completed_not_true"
                    )
                if admission.get("blind_evaluation_passed") is not False:
                    errors.append(
                        f"{prefix}.admission.blind_evaluation_passed_must_be_false"
                    )
                if checks.get("data_window_feasibility_passed") is not True:
                    errors.append(
                        f"{prefix}.admission.data_window_feasibility_not_true"
                    )
            elif checks.get("data_window_feasibility_passed") is not True:
                errors.append(
                    f"{prefix}.admission.data_window_feasibility_not_true"
                )
            if status == "production_eligible":
                errors.append(
                    f"{prefix}.production_evidence_schema_not_implemented"
                )
                if admission.get("production_admitted") is not True:
                    errors.append(
                        f"{prefix}.admission.production_admitted_not_true"
                    )
                for check in (
                    "cost_inclusive_backtest_passed",
                    "independent_reproduction_passed",
                ):
                    if checks.get(check) is not True:
                        errors.append(f"{prefix}.admission.{check}_not_true")
        elif status == "data_pending":
            checks = admission.get("checks") or {}
            if admission.get("backtest_admitted") is not False:
                errors.append(
                    f"{prefix}.admission.backtest_admitted_must_be_false"
                )
            if admission.get("production_admitted") is not False:
                errors.append(
                    f"{prefix}.admission.production_admitted_must_be_false"
                )
            if checks.get("data_window_feasibility_passed") is not False:
                errors.append(
                    f"{prefix}.admission.data_window_feasibility_must_be_false"
                )
        elif status == "implementation_pending":
            checks = admission.get("checks") or {}
            authorized = admission.get("implementation_authorized") is True
            for field in ("backtest_admitted", "production_admitted"):
                if admission.get(field) is not False:
                    errors.append(f"{prefix}.admission.{field}_must_be_false")
            if admission.get("implementation_built", False) is not False:
                errors.append(
                    f"{prefix}.admission.implementation_built_must_be_false"
                )
            if admission.get("implementation_admitted") is not False:
                errors.append(
                    f"{prefix}.admission.implementation_admitted_must_be_false"
                )
            for field in (
                "candidate_signal_computed",
                "candidate_outcome_computed",
                "price_delay_computed",
                "ic_or_gate_computed",
                "mining_or_backtest_run",
                "paid_api_used",
            ):
                if admission.get(field) is not False:
                    errors.append(f"{prefix}.admission.{field}_must_be_false")
            if authorized:
                if not amendment:
                    errors.append(
                        f"{prefix}.admission.implementation_authorization_requires_amendment"
                    )
                prebuild_state = amendment.get("prebuild_admission_state") or {}
                if prebuild_state.get("implementation_authorized") is not True:
                    errors.append(
                        f"{prefix}.admission.prebuild_authorization_mismatch"
                    )
                frozen_tests = amendment.get("effective_frozen_tests") or {}
                if frozen_tests.get("implementation_allowed") is not True:
                    errors.append(
                        f"{prefix}.admission.implementation_not_allowed_by_amendment"
                    )
                for field in ("backtest_allowed", "production_allowed"):
                    if frozen_tests.get(field) is not False:
                        errors.append(
                            f"{prefix}.admission.{field}_must_be_false"
                        )
                if feasibility_passed is not True:
                    errors.append(
                        f"{prefix}.admission.input_feasibility_not_passed"
                    )
                for check in (
                    "data_window_feasibility_passed",
                    "implementation_scope_frozen",
                    "implementation_not_started",
                ):
                    if checks.get(check) is not True:
                        errors.append(f"{prefix}.admission.{check}_not_true")
                for check in (
                    "synthetic_and_pit_tests_passed",
                    "implementation_hashes_frozen",
                ):
                    if checks.get(check) is not False:
                        errors.append(f"{prefix}.admission.{check}_must_be_false")
                implementation_spec = amendment.get(
                    "effective_implementation_spec"
                ) or {}
                expected_scope_files = (
                    list(implementation_spec.get("target_files") or [])
                    + list(implementation_spec.get("test_files") or [])
                )
                authorized_scope = admission.get("authorized_scope") or {}
                if authorized_scope.get("files") != expected_scope_files:
                    errors.append(
                        f"{prefix}.admission.authorized_scope_files_mismatch"
                    )
                if authorized_scope.get("may_use_only_synthetic_values") is not True:
                    errors.append(
                        f"{prefix}.admission.synthetic_only_not_true"
                    )
                for field in (
                    "implementation_sha256",
                    "implementation_files",
                    "implementation_audit_path",
                    "implementation_audit_sha256",
                ):
                    if admission.get(field) is not None:
                        errors.append(
                            f"{prefix}.admission.{field}_forbidden_before_build"
                        )
            else:
                expected_feasibility = feasibility_passed is True
                if (
                    checks.get("data_window_feasibility_passed")
                    is not expected_feasibility
                ):
                    errors.append(
                        f"{prefix}.admission.data_window_feasibility_mismatch"
                    )
    return errors, warnings


def verify_candidate(
    candidate: Mapping[str, Any] | str,
    catalog: Mapping[str, Any] | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    active_catalog = _catalog(catalog)
    errors: list[str] = []
    if isinstance(candidate, str):
        resolved = _candidate_by_id(candidate, active_catalog)
        if resolved is None:
            candidate_value: dict[str, Any] = {"candidate_id": candidate}
            errors.append(f"candidate[{candidate}].not_in_catalog")
        else:
            candidate_value = resolved
    elif isinstance(candidate, Mapping):
        candidate_value = dict(candidate)
    else:
        candidate_value = {}
        errors.append("candidate_not_object_or_id")
    candidate_id = candidate_value.get("candidate_id")
    canonical_candidate = _candidate_by_id(str(candidate_id or ""), active_catalog)
    if canonical_candidate is None:
        marker = candidate_id if isinstance(candidate_id, str) else "?"
        error = f"candidate[{marker}].not_in_catalog"
        if error not in errors:
            errors.append(error)
    elif candidate_value != canonical_candidate:
        errors.append(f"candidate[{candidate_id}].catalog_mismatch")
    field_errors, warnings = _verify_candidate_fields(
        candidate_value,
        active_catalog,
        root,
    )
    errors.extend(field_errors)
    record = record_for_candidate(candidate_value, active_catalog)
    record_sha256 = _sha256_json(record)
    status = candidate_value.get("status") or "invalid"
    valid = not errors
    runnable = bool(valid and candidate_value.get("runnable") is True)
    preregistration = candidate_value.get("preregistration") or {}
    return {
        "candidate_id": candidate_value.get("candidate_id"),
        "valid": valid,
        "status": status,
        "runnable": runnable,
        "errors": errors,
        "warnings": warnings,
        "source_ids": list(candidate_value.get("source_ids") or []),
        "protocol_sha256": preregistration.get("protocol_sha256"),
        "admission_sha256": preregistration.get("admission_sha256"),
        "research_record": record,
        "research_record_sha256": record_sha256,
    }


def _blind_evaluator_fingerprint(
    runner_sources: list[dict[str, Any]],
    config_values: Mapping[str, Any],
    runtime: Mapping[str, Any],
) -> str:
    return _sha256_json({
        "runner_sources": runner_sources,
        "config_values": dict(config_values),
        "runtime": dict(runtime),
    })


def _blind_evaluator_config_values() -> dict[str, Any]:
    return {
        key: deepcopy(getattr(config, key))
        for key in _BLIND_EVALUATOR_CONFIG_KEYS
    }


def _blind_evaluator_runtime() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        **{
            key: metadata.version(distribution)
            for key, distribution in _BLIND_EVALUATOR_RUNTIME_PACKAGES.items()
        },
    }


def verify_blind_authorization(
    path: object,
    expected_sha256: object,
    catalog: Mapping[str, Any] | None = None,
    root: Path = PROJECT_ROOT,
    *,
    verify_panel: bool,
) -> dict[str, Any]:
    """Verify a caller-named authorization before or after run reservation.

    ``verify_panel=False`` is intentionally lexical-only for the large Panel
    input.  The caller must durably reserve the run before repeating the same
    authorization check with ``verify_panel=True``.
    """
    active_catalog = _catalog(catalog)
    errors: list[str] = []
    authorization: dict[str, Any] = {}
    candidate_result: dict[str, Any] = {}
    authorization_sha256 = (
        expected_sha256
        if isinstance(expected_sha256, str)
        and _HEX64.fullmatch(expected_sha256)
        else None
    )
    if type(verify_panel) is not bool:
        errors.append("blind_authorization.verify_panel_not_boolean")
    if authorization_sha256 is None:
        errors.append("blind_authorization.expected_sha256_invalid")
    expected_path = (
        f"research/blind_authorizations/{authorization_sha256}.json"
        if authorization_sha256 is not None else None
    )
    if path != expected_path:
        errors.append("blind_authorization.path_not_content_addressed")
    elif expected_path is not None:
        authorization = _local_artifact(
            root,
            expected_path,
            authorization_sha256,
            "blind_authorization",
            errors,
        )

    if not authorization:
        return {
            "valid": False,
            "errors": errors,
            "authorization": authorization,
            "candidate_result": candidate_result,
            "authorization_sha256": authorization_sha256,
            "input_verified": False,
        }

    expected_authorization_fields = {
        "schema_version",
        "action",
        "max_executions",
        "manual_invocation_only",
        "daemon_discovery_allowed",
        "candidate",
        "chain",
        "implementation",
        "evaluator",
        "input",
        "execution",
        "permissions",
        *_BLIND_AUTHORIZATION_FALSE_FIELDS,
    }
    if set(authorization) != expected_authorization_fields:
        errors.append("blind_authorization.fields_mismatch")
    if type(authorization.get("schema_version")) is not int or (
        authorization.get("schema_version") != 1
    ):
        errors.append("blind_authorization.schema_version_mismatch")
    if authorization.get("action") != "blind_discovery_validation_once":
        errors.append("blind_authorization.action_mismatch")
    if type(authorization.get("max_executions")) is not int or (
        authorization.get("max_executions") != 1
    ):
        errors.append("blind_authorization.max_executions_mismatch")
    if authorization.get("manual_invocation_only") is not True:
        errors.append("blind_authorization.manual_invocation_only_mismatch")
    if authorization.get("daemon_discovery_allowed") is not False:
        errors.append("blind_authorization.daemon_discovery_allowed_mismatch")

    candidate_binding = authorization.get("candidate")
    if not isinstance(candidate_binding, Mapping):
        errors.append("blind_authorization.candidate_not_object")
        candidate_binding = {}
    if set(candidate_binding) != {
        "id", "research_record_sha256", "multiple_testing_family_id",
    }:
        errors.append("blind_authorization.candidate.fields_mismatch")
    candidate_id = candidate_binding.get("id")
    if not isinstance(candidate_id, str) or not candidate_id:
        errors.append("blind_authorization.candidate.id_invalid")
    candidate_result = verify_candidate(
        str(candidate_id or ""), active_catalog, root,
    )
    if candidate_result.get("valid") is not True:
        errors.append("blind_authorization.candidate.current_record_invalid")
        errors.extend(
            f"blind_authorization.candidate:{error}"
            for error in candidate_result.get("errors") or []
        )
    if (
        candidate_result.get("status") != _SEALED_IMPLEMENTATION_STATUS
        or candidate_result.get("runnable") is not False
    ):
        errors.append("blind_authorization.candidate.not_built_and_locked")
    current_candidate = _candidate_by_id(str(candidate_id or ""), active_catalog)
    current_candidate = current_candidate or {}
    if candidate_binding.get("research_record_sha256") != candidate_result.get(
        "research_record_sha256"
    ):
        errors.append(
            "blind_authorization.candidate.research_record_sha256_mismatch"
        )
    if candidate_binding.get("multiple_testing_family_id") != (
        current_candidate.get("multiple_testing_family_id")
    ):
        errors.append(
            "blind_authorization.candidate.multiple_testing_family_id_mismatch"
        )

    preregistration = current_candidate.get("preregistration") or {}
    implementation = current_candidate.get("implementation") or {}
    amendment = _local_artifact(
        root,
        preregistration.get("amendment_path"),
        preregistration.get("amendment_sha256"),
        "blind_authorization.current_amendment",
        errors,
    )
    chain = authorization.get("chain")
    if not isinstance(chain, Mapping):
        errors.append("blind_authorization.chain_not_object")
        chain = {}
    target_contract = (
        amendment.get("effective_implementation_spec") or {}
    ).get("target_contract") or {}
    expected_chain = {
        "amendment_path": preregistration.get("amendment_path"),
        "amendment_sha256": preregistration.get("amendment_sha256"),
        "admission_path": preregistration.get("admission_path"),
        "admission_sha256": preregistration.get("admission_sha256"),
        "implementation_audit_path": implementation.get("audit_path"),
        "implementation_audit_sha256": implementation.get("audit_sha256"),
        "target_contract_sha256": target_contract.get("semantics_sha256"),
        "blind_evaluation_guard": amendment.get("blind_evaluation_guard"),
    }
    if set(chain) != set(expected_chain):
        errors.append("blind_authorization.chain.fields_mismatch")
    if chain != expected_chain:
        errors.append("blind_authorization.chain_mismatch")
    _verify_blind_guard_identity(
        root,
        chain.get("blind_evaluation_guard"),
        "blind_authorization.chain.blind_evaluation_guard",
        errors,
    )

    expected_implementation = [
        {"path": item.get("path"), "sha256": item.get("sha256")}
        for item in _implementation_files(implementation)
    ]
    authorization_implementation = authorization.get("implementation")
    if not isinstance(authorization_implementation, Mapping):
        errors.append("blind_authorization.implementation_not_object")
        authorization_implementation = {}
    if set(authorization_implementation) != {"files"}:
        errors.append("blind_authorization.implementation.fields_mismatch")
    implementation_files = _ordered_physical_identity(
        root,
        authorization_implementation.get("files"),
        [item["path"] for item in expected_implementation],
        "blind_authorization.implementation.files",
        errors,
    )
    if implementation_files != expected_implementation:
        errors.append("blind_authorization.implementation.files_mismatch")

    evaluator = authorization.get("evaluator")
    if not isinstance(evaluator, Mapping):
        errors.append("blind_authorization.evaluator_not_object")
        evaluator = {}
    expected_evaluator_fields = {
        "fingerprint",
        "research_epoch",
        "l0_contract_path",
        "l0_contract_sha256",
        "runner_sources",
        "config_values",
        "runtime",
    }
    if set(evaluator) != expected_evaluator_fields:
        errors.append("blind_authorization.evaluator.fields_mismatch")
    runner_sources = _ordered_physical_identity(
        root,
        evaluator.get("runner_sources"),
        _BLIND_EVALUATOR_PATHS,
        "blind_authorization.evaluator.runner_sources",
        errors,
    )
    implementation_by_path = {
        item["path"]: item for item in expected_implementation
    }
    runner_by_path = {item["path"]: item for item in runner_sources}
    for path, identity in implementation_by_path.items():
        if path in _BLIND_EVALUATOR_PATHS and runner_by_path.get(path) != identity:
            errors.append(
                "blind_authorization.evaluator.runner_sources_mismatch"
            )
            break
    evaluator_binding = amendment.get("evaluator_contract") or {}
    if evaluator.get("research_epoch") != evaluator_binding.get(
        "research_epoch"
    ):
        errors.append("blind_authorization.evaluator.research_epoch_mismatch")
    if (
        evaluator.get("l0_contract_path") != evaluator_binding.get("path")
        or evaluator.get("l0_contract_sha256")
        != evaluator_binding.get("sha256")
    ):
        errors.append("blind_authorization.evaluator.l0_contract_mismatch")
    _local_file(
        root,
        evaluator.get("l0_contract_path"),
        evaluator.get("l0_contract_sha256"),
        "blind_authorization.evaluator.l0_contract",
        errors,
    )
    config_values = evaluator.get("config_values")
    expected_config_values = _blind_evaluator_config_values()
    if _canonical_json(config_values) != _canonical_json(expected_config_values):
        errors.append("blind_authorization.evaluator.config_values_mismatch")
    runtime = evaluator.get("runtime")
    try:
        expected_runtime = _blind_evaluator_runtime()
    except metadata.PackageNotFoundError:
        expected_runtime = {}
        errors.append("blind_authorization.evaluator.runtime_unavailable")
    if runtime != expected_runtime:
        errors.append("blind_authorization.evaluator.runtime_mismatch")
    fingerprint = None
    if (
        len(runner_sources) == len(_BLIND_EVALUATOR_PATHS)
        and isinstance(config_values, Mapping)
        and isinstance(runtime, Mapping)
    ):
        fingerprint = _blind_evaluator_fingerprint(
            runner_sources, config_values, runtime,
        )
    if evaluator.get("fingerprint") != fingerprint:
        errors.append("blind_authorization.evaluator.fingerprint_mismatch")

    input_binding = authorization.get("input")
    if not isinstance(input_binding, Mapping):
        errors.append("blind_authorization.input_not_object")
        input_binding = {}
    if set(input_binding) != {
        "panel_cache_path",
        "panel_cache_sha256",
        "panel_cache_key",
        "panel_data_key",
    }:
        errors.append("blind_authorization.input.fields_mismatch")
    panel_path = input_binding.get("panel_cache_path")
    panel_sha256 = input_binding.get("panel_cache_sha256")
    panel_path_valid = _safe_relative_path(panel_path) and (
        PurePosixPath(str(panel_path)).parts[:2]
        == ("factor_system", "cache")
        and PurePosixPath(str(panel_path)).suffix == ".panel"
    )
    if not panel_path_valid:
        errors.append("blind_authorization.input.panel_cache_path_invalid")
    if not isinstance(panel_sha256, str) or not _HEX64.fullmatch(panel_sha256):
        errors.append("blind_authorization.input.panel_cache_sha256_invalid")
    panel_file = None
    if verify_panel is True and panel_path_valid and isinstance(
        panel_sha256, str
    ) and _HEX64.fullmatch(panel_sha256):
        panel_file = _local_file(
            root,
            panel_path,
            panel_sha256,
            "blind_authorization.input.panel_cache",
            errors,
        )
    key = input_binding.get("panel_cache_key")
    if (
        not isinstance(key, str)
        or not key
        or not isinstance(panel_path, str)
        or PurePosixPath(panel_path).name != key
    ):
        errors.append("blind_authorization.input.panel_cache_key_mismatch")
    expected_panel_data_key = (
        "blind-panel-v1:"
        f"{key}:{panel_sha256}"
        if isinstance(key, str) and key else None
    )
    if input_binding.get("panel_data_key") != expected_panel_data_key:
        errors.append("blind_authorization.input.panel_data_key_invalid")

    execution = authorization.get("execution")
    if not isinstance(execution, Mapping):
        errors.append("blind_authorization.execution_not_object")
        execution = {}
    if set(execution) != {
        "entrypoint", "seed", "exact_stage", "max_scientific_trials",
        "pass_status", "fail_status",
    }:
        errors.append("blind_authorization.execution.fields_mismatch")
    expected_execution = {
        "entrypoint": "factor_system.pipeline.run_blind_candidate_once",
        "exact_stage": "discovery_validation_l0_l3_only",
        "max_scientific_trials": 1,
        "pass_status": "validated_locked",
        "fail_status": "rejected",
    }
    for field, expected in expected_execution.items():
        observed = execution.get(field)
        if (
            field == "max_scientific_trials"
            and (type(observed) is not int or observed != expected)
        ) or (field != "max_scientific_trials" and observed != expected):
            errors.append(f"blind_authorization.execution.{field}_mismatch")
    seed = execution.get("seed")
    if isinstance(seed, bool) or not isinstance(seed, int):
        errors.append("blind_authorization.execution.seed_invalid")
    permissions = authorization.get("permissions")
    if (
        not isinstance(permissions, Mapping)
        or set(permissions) != set(_BLIND_AUTHORIZATION_PERMISSIONS)
        or any(
            permissions.get(field) is not expected
            for field, expected in _BLIND_AUTHORIZATION_PERMISSIONS.items()
        )
    ):
        errors.append("blind_authorization.permissions_invalid")
    for field in _BLIND_AUTHORIZATION_FALSE_FIELDS:
        if authorization.get(field) is not False:
            errors.append(f"blind_authorization.{field}_must_be_false")

    return {
        "valid": not errors,
        "errors": errors,
        "authorization": authorization,
        "candidate_result": candidate_result,
        "authorization_sha256": authorization_sha256,
        "input_verified": bool(
            verify_panel is True and panel_file is not None and not errors
        ),
    }


def verify_catalog(
    catalog: Mapping[str, Any] | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    active_catalog = _catalog(catalog)
    errors: list[str] = []
    warnings: list[str] = []
    if active_catalog.get("schema_version") != 1:
        errors.append("catalog.schema_version_invalid")
    if not isinstance(active_catalog.get("catalog_id"), str) or not (
        active_catalog["catalog_id"].strip()
    ):
        errors.append("catalog.catalog_id_missing")
    policy = active_catalog.get("catalog_policy")
    if not isinstance(policy, dict):
        errors.append("catalog.policy_missing")
        policy = {}
    allowed_authorities = set(policy.get("allowed_source_authorities") or [])
    if allowed_authorities != {
        "primary_paper",
        "official_research_repository",
    }:
        errors.append("catalog.allowed_source_authorities_invalid")
    if policy.get("prohibit_paid_api") is not True:
        errors.append("catalog.prohibit_paid_api_must_be_true")
    if policy.get("prohibit_upstream_result_data") is not True:
        errors.append("catalog.prohibit_upstream_result_data_must_be_true")
    sources = active_catalog.get("sources")
    if not isinstance(sources, list) or not sources:
        errors.append("catalog.sources_empty")
        sources = []
    source_ids: list[str] = []
    for source in sources:
        errors.extend(_verify_source(source, allowed_authorities))
        if isinstance(source, dict) and isinstance(source.get("source_id"), str):
            source_ids.append(source["source_id"])
    if len(source_ids) != len(set(source_ids)):
        errors.append("catalog.source_ids_duplicate")
    policies = active_catalog.get("house_policies")
    if not isinstance(policies, list) or not policies:
        errors.append("catalog.house_policies_empty")
    else:
        for index, item in enumerate(policies):
            if not isinstance(item, dict) or item.get("basis") != "house_policy":
                errors.append(f"catalog.house_policies[{index}].basis_invalid")
    candidates = active_catalog.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        errors.append("catalog.candidates_empty")
        candidates = []
    candidate_ids: list[str] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            errors.append("catalog.candidate_not_object")
            continue
        candidate_ids.append(str(candidate.get("candidate_id") or ""))
        result = verify_candidate(candidate, active_catalog, root)
        errors.extend(result["errors"])
        warnings.extend(result["warnings"])
    if len(candidate_ids) != len(set(candidate_ids)):
        errors.append("catalog.candidate_ids_duplicate")
    superseded = active_catalog.get("superseded_non_outcome_trials")
    if superseded is None:
        superseded = []
    elif not isinstance(superseded, list):
        errors.append("catalog.superseded_non_outcome_trials_not_list")
        superseded = []
    superseded_ids: list[str] = []
    candidate_map = {
        str(item.get("candidate_id")): item
        for item in candidates if isinstance(item, dict)
    }
    raw_archives = active_catalog.get("archived_prior_tests")
    archived_identity_map: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw_archives, list):
        for archive in raw_archives:
            if not isinstance(archive, dict):
                continue
            original_identity = archive.get("original_identity")
            if not isinstance(original_identity, Mapping):
                continue
            archived_candidate_id = original_identity.get("candidate_id")
            if isinstance(archived_candidate_id, str):
                archived_identity_map[archived_candidate_id] = original_identity
    for index, item in enumerate(superseded):
        prefix = f"catalog.superseded_non_outcome_trial[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}_not_object")
            continue
        errors.extend(_verify_prior_trial_identity(item, prefix))
        candidate_id = str(item.get("candidate_id") or "")
        replacement_id = str(item.get("superseded_by") or "")
        superseded_ids.append(candidate_id)
        if not isinstance(item.get("multiple_testing_family_id"), str) or not (
            item["multiple_testing_family_id"].strip()
        ):
            errors.append(f"{prefix}.multiple_testing_family_id_missing")
        original = candidate_map.get(candidate_id)
        archived_original = archived_identity_map.get(candidate_id)
        replacement = candidate_map.get(replacement_id)
        archived_replacement = archived_identity_map.get(replacement_id)
        if original is None:
            if archived_original is None:
                errors.append(f"{prefix}.candidate_missing")
            else:
                for field in _PRIOR_TRIAL_IDENTITY_FIELDS:
                    if item.get(field) != archived_original.get(field):
                        errors.append(f"{prefix}.{field}_archive_mismatch")
        elif (
            original.get("status") != item.get("status")
            or original.get("runnable") is not False
            or original.get("result_data_read") is not False
        ):
            errors.append(f"{prefix}.candidate_state_mismatch")
        if (
            (replacement is None and archived_replacement is None)
            or replacement_id == candidate_id
        ):
            errors.append(f"{prefix}.replacement_invalid")
        if item.get("result_data_read") is not False:
            errors.append(f"{prefix}.result_data_read_forbidden")
        if item.get("runnable") is not False:
            errors.append(f"{prefix}.runnable_must_be_false")
        if not isinstance(item.get("reason"), str) or not item["reason"].strip():
            errors.append(f"{prefix}.reason_missing")
    if len(superseded_ids) != len(set(superseded_ids)):
        errors.append("catalog.superseded_non_outcome_trial_ids_duplicate")
    contaminated = active_catalog.get("contaminated_trials")
    if not isinstance(contaminated, list):
        errors.append("catalog.contaminated_trials_not_list")
        contaminated = []
    trial_ids: list[str] = []
    for index, trial in enumerate(contaminated):
        if not isinstance(trial, dict):
            errors.append(f"catalog.contaminated_trial[{index}]_not_object")
            continue
        errors.extend(_verify_prior_trial_identity(
            trial, f"catalog.contaminated_trial[{index}]",
        ))
        errors.extend(_verify_lifecycle_resolution(
            trial, root, f"catalog.contaminated_trial[{index}]",
        ))
        trial_ids.append(str(trial.get("trial_id") or ""))
        if not isinstance(trial.get("multiple_testing_family_id"), str) or not (
            trial["multiple_testing_family_id"].strip()
        ):
            errors.append(
                f"catalog.contaminated_trial[{index}].multiple_testing_family_id_missing"
            )
        if not isinstance(trial.get("superseded_by"), str) or not trial[
            "superseded_by"
        ].strip():
            errors.append(
                f"catalog.contaminated_trial[{index}].superseded_by_missing"
            )
        if trial.get("status") != "rejected_after_outcome_observed":
            errors.append(f"catalog.contaminated_trial[{index}].status_invalid")
        if trial.get("runnable") is not False:
            errors.append(
                f"catalog.contaminated_trial[{index}].runnable_must_be_false"
            )
        if trial.get("result_data_read") is not True:
            errors.append(
                f"catalog.contaminated_trial[{index}].result_data_read_must_be_true"
            )
        if trial.get("evidence_basis") != "house_policy":
            errors.append(
                f"catalog.contaminated_trial[{index}].evidence_basis_invalid"
            )
        if not isinstance(trial.get("reason"), str) or not trial["reason"].strip():
            errors.append(f"catalog.contaminated_trial[{index}].reason_missing")
        if not isinstance(trial.get("research_record_sha256"), str) or not (
            _HEX64.fullmatch(trial["research_record_sha256"])
        ):
            errors.append(
                f"catalog.contaminated_trial[{index}].research_record_sha256_invalid"
            )
    if len(trial_ids) != len(set(trial_ids)):
        errors.append("catalog.contaminated_trial_ids_duplicate")
    archives = raw_archives
    if not isinstance(archives, list):
        errors.append("catalog.archived_prior_tests_not_list")
        archives = []
    archive_ids: list[str] = []
    archived_candidate_ids: list[str] = []
    for index, archive in enumerate(archives):
        if not isinstance(archive, dict):
            errors.append(f"catalog.archive[{index}]_not_object")
            continue
        errors.extend(_verify_prior_trial_identity(
            archive, f"catalog.archive[{index}]",
        ))
        archive_ids.append(str(archive.get("archive_id") or ""))
        if archive.get("status") != "archived_prior_test":
            errors.append(f"catalog.archive[{index}].status_invalid")
        if archive.get("runnable") is not False:
            errors.append(f"catalog.archive[{index}].runnable_must_be_false")
        if archive.get("result_data_read") is not False:
            errors.append(f"catalog.archive[{index}].result_data_read_forbidden")
        if archive.get("evidence_basis") != "house_policy":
            errors.append(f"catalog.archive[{index}].evidence_basis_invalid")
        original_identity = archive.get("original_identity")
        if original_identity is not None:
            if not isinstance(original_identity, Mapping):
                errors.append(
                    f"catalog.archive[{index}].original_identity_not_object"
                )
            else:
                errors.extend(_verify_prior_trial_identity(
                    original_identity,
                    f"catalog.archive[{index}].original_identity",
                ))
                original_candidate_id = original_identity.get("candidate_id")
                archived_candidate_ids.append(str(original_candidate_id or ""))
                if original_candidate_id != archive.get("candidate_id"):
                    errors.append(
                        f"catalog.archive[{index}].original_identity_candidate_mismatch"
                    )
    if len(archive_ids) != len(set(archive_ids)):
        errors.append("catalog.archive_ids_duplicate")
    if len(archived_candidate_ids) != len(set(archived_candidate_ids)):
        errors.append("catalog.archive_original_candidate_ids_duplicate")
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "catalog_sha256": _sha256_json(active_catalog),
        "source_count": len(sources),
        "candidate_count": len(candidates),
    }


def catalog_summary(
    catalog: Mapping[str, Any] | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    active_catalog = _catalog(catalog)
    verification = verify_catalog(active_catalog, root)
    candidates = [
        item for item in (active_catalog.get("candidates") or [])
        if isinstance(item, dict)
    ]
    archives = [
        item for item in (active_catalog.get("archived_prior_tests") or [])
        if isinstance(item, dict)
    ]
    contaminated = [
        item for item in (active_catalog.get("contaminated_trials") or [])
        if isinstance(item, dict)
    ]
    return {
        **verification,
        "evidence_based_count": sum(
            item.get("evidence_basis") == "evidence_based"
            for item in candidates
        ),
        "implementation_pending_count": sum(
            item.get("status") == "implementation_pending"
            for item in candidates
        ),
        "data_pending_count": sum(
            item.get("status") == "data_pending" for item in candidates
        ),
        "blind_rejected_count": sum(
            item.get("status") == _BLIND_REJECTED_STATUS
            for item in candidates
        ),
        "runnable_count": sum(item.get("runnable") is True for item in candidates),
        "contaminated_trial_count": len(contaminated),
        "archived_prior_test_count": len(archives),
    }


def preflight_candidates(
    candidates: Iterable[Mapping[str, Any] | str],
    catalog: Mapping[str, Any] | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    active_catalog = _catalog(catalog)
    results = [
        verify_candidate(candidate, active_catalog, root)
        for candidate in candidates
    ]
    errors = [
        f"{result.get('candidate_id') or '?'}:{error}"
        for result in results
        for error in result["errors"]
    ]
    runnable_ids = [
        result["candidate_id"] for result in results if result["runnable"]
    ]
    blocked_ids = [
        result["candidate_id"] for result in results if not result["runnable"]
    ]
    metadata_valid = bool(results) and not errors
    all_runnable = bool(results) and all(
        result["runnable"] for result in results
    )
    return {
        "valid": metadata_valid and all_runnable,
        "metadata_valid": metadata_valid,
        "all_runnable": all_runnable,
        "candidate_count": len(results),
        "runnable_candidate_ids": runnable_ids,
        "blocked_candidate_ids": blocked_ids,
        "results": results,
        "errors": errors,
    }


def verify_frozen_record(
    row: Mapping[str, Any],
    recipe: Mapping[str, Any],
    catalog: Mapping[str, Any] | None = None,
    root: Path = PROJECT_ROOT,
) -> dict[str, Any]:
    active_catalog = _catalog(catalog)
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(row, Mapping) or not isinstance(recipe, Mapping):
        return {
            "candidate_id": None,
            "record_status": "invalid",
            "valid": False,
            "status": "invalid",
            "runnable": False,
            "errors": ["row_and_recipe_must_be_objects"],
            "warnings": [],
            "research_record": None,
            "research_record_sha256": None,
        }
    row_record = row.get("research_record")
    recipe_record = recipe.get("research_record")
    candidate_id = (
        row.get("source_candidate_id")
        or recipe.get("source_candidate_id")
        or (
            (row_record.get("candidate") or {}).get("candidate_id")
            if isinstance(row_record, dict)
            else None
        )
    )
    record_status = row.get("status") or recipe.get("status") or "invalid"
    if record_status == "archived_prior_test":
        archive_id = (
            row.get("archive_id")
            or recipe.get("archive_id")
            or (
                (row_record.get("archive") or {}).get("archive_id")
                if isinstance(row_record, dict)
                else None
            )
        )
        archive = _archive_by_id(str(archive_id or ""), active_catalog)
        if archive is None:
            errors.append("archive_not_in_catalog")
            canonical_record = None
        else:
            canonical_record = _archive_record(archive, active_catalog)
        candidate_id = archive_id
        base_runnable = False
        base_valid = archive is not None
        if row.get("runnable") is not False or recipe.get("runnable") is not False:
            errors.append("archived_prior_test_must_not_be_runnable")
    else:
        candidate = _candidate_by_id(str(candidate_id or ""), active_catalog)
        if candidate is None:
            errors.append("candidate_not_in_catalog")
            canonical_record = None
            base_valid = False
            base_runnable = False
        else:
            candidate_result = verify_candidate(candidate, active_catalog, root)
            errors.extend(candidate_result["errors"])
            warnings.extend(candidate_result["warnings"])
            canonical_record = candidate_result["research_record"]
            base_valid = candidate_result["valid"]
            base_runnable = candidate_result["runnable"]
            if record_status != candidate_result["status"]:
                errors.append("frozen_record_status_mismatch")
    canonical_sha256 = (
        _sha256_json(canonical_record)
        if canonical_record is not None
        else None
    )
    for label, holder in (("row", row), ("recipe", recipe)):
        record = holder.get("research_record")
        digest = holder.get("research_record_sha256")
        if not isinstance(record, dict):
            errors.append(f"{label}.research_record_missing")
            continue
        actual_digest = _sha256_json(record)
        if not isinstance(digest, str) or not _HEX64.fullmatch(digest):
            errors.append(f"{label}.research_record_sha256_invalid")
        elif digest != actual_digest:
            errors.append(f"{label}.research_record_sha256_mismatch")
        if canonical_record is not None and record != canonical_record:
            errors.append(f"{label}.research_record_catalog_mismatch")
        if canonical_sha256 is not None and digest != canonical_sha256:
            errors.append(f"{label}.research_record_catalog_sha256_mismatch")
    if row_record != recipe_record:
        errors.append("row_recipe_research_record_mismatch")
    if row.get("research_record_sha256") != recipe.get(
        "research_record_sha256"
    ):
        errors.append("row_recipe_research_record_sha256_mismatch")
    valid = bool(base_valid and not errors)
    return {
        "candidate_id": candidate_id,
        "record_status": record_status,
        "valid": valid,
        "status": record_status,
        "runnable": bool(valid and base_runnable),
        "errors": errors,
        "warnings": warnings,
        "research_record": canonical_record,
        "research_record_sha256": canonical_sha256,
    }


__all__ = [
    "CATALOG_PATH",
    "PROJECT_ROOT",
    "SourceCatalogError",
    "catalog_summary",
    "load_catalog",
    "preflight_candidates",
    "record_for_candidate",
    "verify_blind_authorization",
    "verify_candidate",
    "verify_catalog",
    "verify_frozen_record",
]
