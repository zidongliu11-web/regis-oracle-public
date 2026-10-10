"""Explicit frozen native input entry; integrity is separate from research qualification.

The caller supplies a previously authorized snapshot. This module neither discovers
legacy panels/registries nor grants source rights, PIT coverage or model admission.
"""
from __future__ import annotations
import hashlib
import os
from pathlib import Path, PureWindowsPath
import re
import stat
import pandas as pd
from .data import Panel, _read_panel_cache


class NativeSnapshotError(ValueError):
    pass


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size,
            info.st_mtime_ns, info.st_ctime_ns)


def _path_identity(info):
    # Windows 3.12 lstat/fstat disagree on deprecated ctime for an unchanged
    # file. Retain ctime for comparisons within the same open descriptor.
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)


def _no_links(base: Path, relative: Path):
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink() or current.is_junction():
            raise NativeSnapshotError("native_snapshot_link_forbidden")
        os.lstat(current)


def _native_root(root):
    if not isinstance(root, (str, Path)) or not str(root):
        raise NativeSnapshotError("native_snapshot_root_invalid")
    supplied = Path(root)
    configured = os.environ.get("LOCALAPPDATA", "")
    if not supplied.is_absolute() or not configured:
        raise NativeSnapshotError("native_snapshot_root_invalid")
    if not Path(configured).is_absolute():
        raise NativeSnapshotError("native_snapshot_root_invalid")
    try:
        base = Path(configured).resolve(strict=True)
    except OSError as exc:
        raise NativeSnapshotError("native_snapshot_root_invalid") from exc
    if not base.is_absolute() or base == Path(base.anchor):
        raise NativeSnapshotError("native_snapshot_root_invalid")
    # Check the lexical root before resolving; a sync-folder alias cannot become
    # trusted merely because its destination is in native storage.
    try:
        relative = supplied.relative_to(base)
        if not relative.parts:
            raise NativeSnapshotError("native_snapshot_root_too_broad")
        _no_links(base, relative)
        resolved = supplied.resolve(strict=True)
        resolved.relative_to(base)
        if resolved == base:
            raise NativeSnapshotError("native_snapshot_root_too_broad")
        if not resolved.is_dir():
            raise NativeSnapshotError("native_snapshot_root_invalid")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, NativeSnapshotError):
            raise
        raise NativeSnapshotError("native_snapshot_root_invalid") from exc
    return resolved


def load_native_panel_snapshot(root: str | Path, relative: str, sha256: str) -> Panel:
    """Read one explicit native archive through one verified descriptor.

    Preserve every research column; executability remains an explicit mask.
    Successful decoding proves frozen physical integrity, never financial readiness.
    """
    if not isinstance(sha256, str) or not re.fullmatch("[0-9a-f]{64}", sha256):
        raise NativeSnapshotError("native_snapshot_sha_invalid")
    if not isinstance(relative, str) or not relative:
        raise NativeSnapshotError("native_snapshot_path_invalid")
    parts = relative.replace("\\", "/").split("/")
    if (PureWindowsPath(relative).drive or relative.startswith(("/", "\\"))
            or any(p in {"", ".", ".."} or p != p.rstrip(" .")
                   or ":" in p or "\0" in p for p in parts)
            or Path(relative).suffix.lower() != ".zip"):
        raise NativeSnapshotError("native_snapshot_path_invalid")
    base = _native_root(root)
    relative_path = Path(*parts)
    target = base / relative_path
    try:
        _no_links(base, relative_path)
        resolved = target.resolve(strict=True)
        resolved.relative_to(base)
        path_before = os.lstat(target)
        if not stat.S_ISREG(path_before.st_mode):
            raise NativeSnapshotError("native_snapshot_not_regular")
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                             | getattr(os, "O_BINARY", 0))
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, NativeSnapshotError):
            raise
        raise NativeSnapshotError("native_snapshot_path_invalid") from exc
    try:
        before = os.fstat(descriptor)
        if (not stat.S_ISREG(before.st_mode)
                or _path_identity(before) != _path_identity(path_before)):
            raise NativeSnapshotError("native_snapshot_identity_changed")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 1024 * 1024):
            digest.update(chunk)
        if _identity(os.fstat(descriptor)) != _identity(before):
            raise NativeSnapshotError("native_snapshot_changed_during_hash")
        if digest.hexdigest() != sha256:
            raise NativeSnapshotError("native_snapshot_sha_mismatch")
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                panel = _read_panel_cache(stream)
                if _identity(os.fstat(descriptor)) != _identity(before):
                    raise NativeSnapshotError("native_snapshot_changed_during_decode")
        except NativeSnapshotError:
            raise
        except Exception as exc:
            raise NativeSnapshotError("native_snapshot_bundle_invalid") from exc
        _no_links(base, relative_path)
        if (_path_identity(os.lstat(target)) != _path_identity(before)
                or target.resolve(strict=True) != resolved):
            raise NativeSnapshotError("native_snapshot_path_replaced")
    finally:
        os.close(descriptor)
    if not isinstance(panel, Panel):
        raise NativeSnapshotError("native_snapshot_panel_invalid")
    close = panel.close
    if (not isinstance(close.index, pd.DatetimeIndex) or not close.index.is_unique
            or close.index.hasnans or not close.index.is_monotonic_increasing
            or not close.index.equals(close.index.normalize())
            or not close.columns.is_unique or close.columns.hasnans
            or not all(isinstance(x, str) and bool(x) for x in close.columns)):
        raise NativeSnapshotError("native_snapshot_axes_invalid")
    panel._native_snapshot = {
        "path": relative_path.as_posix(), "sha256": sha256,
        "physical_integrity_verified": True,
        "source_qualification_verified": False,
        "full_market_coverage_verified": False,
        "model_admitted": False,
    }
    return panel
