"""Validate research artifacts in source or explicitly configured runtime storage."""

from __future__ import annotations

import os
from pathlib import Path


# These are data aliases created by deploy/render_start.sh, not permission to
# load executable code, account files, or arbitrary /var/lib contents.
RUNTIME_AREAS = (Path("reports"), Path("research/data"), Path("research/reports"))


def research_artifact_path(raw: object, *, project_root: Path) -> Path:
    """Return a validated source-relative alias; callers still verify identity.

    Keep serialized evidence references portable between desktop and cloud.
    An external target is trusted only when both REGIS_RUNTIME_ROOT and the
    matching source alias agree. Never learn trust from a symlink's destination.
    """
    supplied = str(raw or "").strip()
    path = Path(supplied)
    if not supplied or ".." in path.parts:
        raise ValueError("research_artifact_path_invalid")
    root = project_root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            # Output packets may not exist yet. strict=True above is still
            # needed: newer Python versions leave symlink loops unresolved
            # in non-strict mode instead of raising an error.
            resolved = candidate.resolve()
        if resolved.is_relative_to(root):
            return resolved
        configured = os.environ.get("REGIS_RUNTIME_ROOT", "").strip()
        runtime_alias = Path(configured)
        if not configured or not runtime_alias.is_absolute():
            raise ValueError("research_runtime_root_not_configured")
        runtime = runtime_alias.resolve()
        if runtime == Path(runtime.anchor):
            raise ValueError("research_runtime_root_too_broad")
        for area in RUNTIME_AREAS:
            if not resolved.is_relative_to(runtime / area):
                continue
            # A source path cannot use an unrelated alias (e.g. code -> data),
            # and runtime subdirectory symlinks cannot escape their own area.
            if not (
                candidate.is_relative_to(root / area)
                or candidate.is_relative_to(runtime / area)
                or candidate.is_relative_to(runtime_alias / area)
            ):
                continue
            relative = resolved.relative_to(runtime)
            alias = root / relative
            if alias.resolve() == resolved:
                return alias
    except (OSError, RuntimeError) as exc:
        raise ValueError("research_artifact_path_unresolvable") from exc
    raise ValueError("research_artifact_path_outside_trusted_roots")
