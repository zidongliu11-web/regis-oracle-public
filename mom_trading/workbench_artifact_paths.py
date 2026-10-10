"""Portable workbench archives inside the existing trusted research-data alias."""
from pathlib import Path

from .research_artifact_paths import research_artifact_path as frozen_path

EXTRA_AREAS = (Path("research/backtest_project/data"), Path("output/research"))


def research_artifact_path(raw, *, project_root):
    native = frozen_path(raw, project_root=project_root)
    if native.exists():
        return native  # Never hide a present but hash-invalid original.
    relative = native.relative_to(project_root.resolve())
    if any(relative.is_relative_to(area) for area in EXTRA_AREAS):
        # Only a storage mapping, not permission to create aliases or migrate
        # data. The frozen validator and job's original SHA still apply.
        return frozen_path(Path('research/data/workbench_archives')/relative, project_root=project_root)
    return native
