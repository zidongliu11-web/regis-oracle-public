"""Native frozen engineering batches, shared computations and full attempt history.

No legacy source discovery, registry restoration, source qualification or admission.
"""
from __future__ import annotations
from collections import OrderedDict
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import inspect
import json
import marshal
import math
import os
from pathlib import Path
import secrets
import sqlite3
import sys
from types import CodeType, FunctionType, ModuleType
import numpy as np
import pandas as pd
from .native_snapshot import load_native_panel_snapshot, _native_root, _identity, _path_identity
from .data import executable_forward_return
from .templates import TEMPLATES
from .metrics import cross_sectional_ic, ic_stats
from .validate import l2_walkforward_forward
from quantqc import ashare as qa

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_PATH_CACHE = {}


class BatchIntegrityError(ValueError):
    pass


class RecipeComputationError(ValueError):
    pass


def _plain(v):
    if isinstance(v, dict): return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)): return [_plain(x) for x in v]
    if isinstance(v, np.generic): return _plain(v.item())
    if isinstance(v, float) and not math.isfinite(v): return None
    return v


def _body(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(raw):
    return hashlib.sha256(raw).hexdigest()


def _normalized_code(code):
    constants = tuple(_normalized_code(c) if isinstance(c, CodeType) else c
                      for c in code.co_consts)
    return code.replace(co_filename="", co_firstlineno=0, co_linetable=b"", co_consts=constants)


def _code_hash(code):
    return _hash(marshal.dumps(_normalized_code(code)))


def _source_path(filename):
    # Code filenames are immutable strings. Cache only path normalization;
    # every source byte and actual live callable hash is still checked anew.
    if filename not in _PATH_CACHE:
        _PATH_CACHE[filename] = Path(filename).resolve()
    return _PATH_CACHE[filename]


def _qa_callables():
    """Reject wrapped/replaced rule slots before accepting underlying code."""
    path = _PROJECT_ROOT / "quantqc" / "ashare.py"
    functions = []
    for name in ("board_of", "_date", "_money", "price_limit_rule", "_limit_price",
                 "_minimum_tick_rule_verified",
                 "opening_auction_executability", "opening_auction_mask"):
        fn = vars(qa).get(name)
        if (not isinstance(fn, FunctionType) or fn.__module__ != "quantqc.ashare"
                or fn.__qualname__ != name or fn.__code__.co_qualname != name
                or _source_path(fn.__code__.co_filename) != path):
            raise BatchIntegrityError("engineering_shared_rules_callable_identity")
        functions.append(fn)
    return functions


def _functions():
    """Track actual project calculation callables without inspecting panel values."""
    project = _PROJECT_ROOT
    queue = [run_batch, _evaluate, executable_forward_return, cross_sectional_ic,
             ic_stats, l2_walkforward_forward, *_qa_callables(),
             *[t.compute for t in TEMPLATES if t.compute]]
    seen, out = set(), []
    while queue:
        fn = queue.pop()
        if not isinstance(fn, FunctionType) or id(fn) in seen:
            continue
        seen.add(id(fn))
        filename = fn.__code__.co_filename
        if filename.startswith("<"):
            continue  # generated dataclass boilerplate, no source expression
        path = _source_path(filename)
        if not path.is_relative_to(project):
            continue  # external library versions are bound separately
        out.append((fn, path))
        names = fn.__code__.co_names
        for name in names:
            obj = fn.__globals__.get(name)
            if isinstance(obj, FunctionType):
                queue.append(obj)
            elif isinstance(obj, ModuleType):
                origin = vars(obj).get("__file__")
                if origin and _source_path(origin).is_relative_to(project):
                    queue.extend(vars(obj)[n] for n in names
                                 if isinstance(vars(obj).get(n), FunctionType))
            elif isinstance(obj, type) and obj.__module__.startswith("factor_system"):
                queue.extend(v for v in vars(obj).values() if isinstance(v, FunctionType))
    return out


def _loaded_codes():
    project = _PROJECT_ROOT
    return {path.relative_to(project).as_posix() + ":" + fn.__qualname__ + ":" + str(fn.__code__.co_firstlineno):
            _code_hash(fn.__code__) for fn, path in _functions()}


def _attest_loaded_disk():
    """Compile without execution; reject already loaded code from another file version."""
    compiled = {}
    for fn, path in _functions():
        if path not in compiled:
            pending = [compile(path.read_bytes(), str(path), "exec", dont_inherit=True,
                               optimize=sys.flags.optimize)]
            signatures = set()
            while pending:
                code = pending.pop()
                signatures.add(_code_hash(code))
                pending.extend(c for c in code.co_consts if isinstance(c, CodeType))
            compiled[path] = signatures
        if _code_hash(fn.__code__) not in compiled[path]:
            raise BatchIntegrityError("engineering_loaded_code_disk_mismatch")


def _sources():
    root = _PROJECT_ROOT / "factor_system"
    files = ["research_batch.py", "native_snapshot.py", "data.py", "templates.py",
             "operators.py", "metrics.py", "validate.py", "config.py", "preprocess.py"]
    file_hashes = {p: _hash((root / p).read_bytes()) for p in files}
    file_hashes["quantqc/stats.py"] = _hash((root.parent / "quantqc/stats.py").read_bytes())
    file_hashes["quantqc/ashare.py"] = _hash((root.parent / "quantqc/ashare.py").read_bytes())
    return {"files": file_hashes,
            "rule_constants": {"board_limits": dict(qa.BOARD_LIMITS),
                               "risk_warning_main_reform_date": qa.RISK_WARNING_MAIN_REFORM_DATE.isoformat(),
                               "risk_warning_before": qa.RISK_WARNING_MAIN_LIMIT_BEFORE_2026_07_06,
                               "risk_warning_after": qa.RISK_WARNING_MAIN_LIMIT_FROM_2026_07_06,
                               "price_tick_yuan": str(qa.PRICE_TICK_YUAN)},
            "runtime": {p: importlib.metadata.version(p) for p in ("numpy", "pandas", "scipy", "pyarrow")},
            "python": sys.version, "loaded_codes": _loaded_codes(),
            "template_domains": {t.tpl_id: _plain(t.params()) for t in TEMPLATES}}


def _check_sources(binding=None):
    current = _sources()
    if (current != _IMPORTED_SOURCES
            or binding is not None and current != binding["sources"]):
        raise BatchIntegrityError("engineering_batch_source_drift")
    return current


def _specification(spec):
    v = json.loads(_body(spec))  # Reject callables/nonfinite JSON before any side effect.
    if (not isinstance(v, dict) or type(v.get("schema")) is not int or v.get("schema") != 1
            or v.get("purpose") != "engineering_only" or v.get("holding_days") != [1, 3, 5]
            or not all(type(x) is int for x in v["holding_days"])):
        raise ValueError("engineering_batch_contract_invalid")
    candidates = v.get("candidates")
    strategies = v.get("reserved_strategy_ids", [])
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 16:
        raise ValueError("engineering_batch_candidate_cap")
    if (not isinstance(strategies, list) or len(strategies) > 3
            or not all(isinstance(x, str) and x for x in strategies)
            or len(set(strategies)) != len(strategies)):
        raise ValueError("engineering_batch_strategy_cap")
    ids = []
    for c in candidates:
        if (not isinstance(c, dict) or not isinstance(c.get("recipe"), dict)
                or c.get("expected_direction") not in (-1, 1)
                or isinstance(c.get("expected_direction"), bool)
                or not all(isinstance(c.get(k), str) and c[k] for k in ("id", "mechanism", "parent_trial"))):
            raise ValueError("engineering_batch_candidate_invalid")
        ids.append(c["id"])
    if len(set(ids)) != len(ids): raise ValueError("engineering_batch_duplicate_candidate")
    try:
        if set(v["periods"]) != {"discovery", "validation"}:
            raise ValueError()
        lo, hi = [pd.Timestamp(x) for x in v["periods"]["discovery"]]
        vlo, vhi = [pd.Timestamp(x) for x in v["periods"]["validation"]]
        if (not lo <= hi < vlo <= vhi or any(pd.isna(x) or x != x.normalize() or x.tzinfo is not None
                                           for x in (lo, hi, vlo, vhi))):
            raise ValueError()
        if not all(isinstance(v["snapshot"][k], str) for k in ("relative", "sha256")):
            raise ValueError()
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("engineering_batch_input_contract_invalid") from exc
    return v


def _unlinked(path):
    if path.is_symlink() or path.is_junction():
        raise BatchIntegrityError("engineering_workbench_link_forbidden")


@contextmanager
def _writer(directory):
    path = directory / "writer.lock"
    _unlinked(path)
    # Advisory rather than existence lock: crash releases lock, stale file harmless.
    with path.open("a+b") as stream:
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise BatchIntegrityError("engineering_workbench_writer_busy") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


_DDL = """
CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, frozen TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trials (batch TEXT, candidate TEXT, hold INTEGER,
 result TEXT NOT NULL, sha TEXT NOT NULL, PRIMARY KEY(batch,candidate,hold));
CREATE TABLE IF NOT EXISTS attempts (id INTEGER PRIMARY KEY, batch TEXT, candidate TEXT,
 hold INTEGER, state TEXT NOT NULL, started TEXT NOT NULL, ended TEXT);
CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, sha TEXT NOT NULL);
"""


class _Context:
    def __init__(self, panel, db, directory, binding):
        self.panel, self.db, self.directory, self.binding = panel, db, directory, binding
        self.memo, self.accessed = OrderedDict(), {}
        self.factor_builds = self.label_builds = self.cache_hits = 0

    def verify(self, sha, decode=False):
        if not isinstance(sha, str) or len(sha) != 64 or any(x not in "0123456789abcdef" for x in sha):
            raise BatchIntegrityError("engineering_cache_reference_invalid")
        path = self.directory / (sha + ".parquet")
        _unlinked(path)
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            while chunk := stream.read(1024 * 1024): digest.update(chunk)
            if digest.hexdigest() != sha:
                raise BatchIntegrityError("engineering_cache_sha_mismatch")
            frame = None
            if decode:
                stream.seek(0)
                frame = pd.read_parquet(stream)
            if (_identity(before) != _identity(os.fstat(stream.fileno()))
                    or _path_identity(before) != _path_identity(os.lstat(path))):
                raise BatchIntegrityError("engineering_cache_changed")
        return frame

    def frame(self, identity, compute):
        _check_sources(self.binding)
        key = _hash(_body({"binding": self.binding, "identity": identity}).encode())
        row = self.db.execute("SELECT sha FROM cache WHERE key=?", (key,)).fetchone()
        if row:
            sha = row[0]
            decoded = self.verify(sha, decode=key not in self.memo)
            frame = self.memo.pop(key) if key in self.memo else decoded
            self.cache_hits += 1
        else:
            frame = compute()
            _check_sources(self.binding)
            if (not isinstance(frame, pd.DataFrame)
                    or not frame.index.equals(self.panel.close.index)
                    or not frame.columns.equals(self.panel.close.columns)):
                raise RecipeComputationError("engineering_computed_axes_invalid")
            temporary = self.directory / ("." + secrets.token_hex(12) + ".tmp")
            try:
                frame.to_parquet(temporary, index=True)
                sha = _hash(temporary.read_bytes())
                target = self.directory / (sha + ".parquet")
                _unlinked(target)
                with temporary.open("r+b") as stream: os.fsync(stream.fileno())
                with self.db:
                    _check_sources(self.binding)
                    if target.exists(): self.verify(sha)
                    else: os.replace(temporary, target)
                    self.db.execute("INSERT INTO cache(key,sha) VALUES(?,?)", (key, sha))
                    _check_sources(self.binding)
            finally:
                temporary.unlink(missing_ok=True)
        if (not frame.index.equals(self.panel.close.index)
                or not frame.columns.equals(self.panel.close.columns)):
            raise BatchIntegrityError("engineering_cached_axes_invalid")
        _check_sources(self.binding)
        self.accessed[key] = sha
        self.memo[key] = frame
        while len(self.memo) > 2: self.memo.popitem(last=False)
        return frame

    def factor(self, c):
        recipe = c["recipe"]
        def compute():
            self.factor_builds += 1
            if recipe.get("kind") != "template":
                raise RecipeComputationError("engineering_recipe_kind_unsupported")
            template = next((t for t in TEMPLATES if t.tpl_id == recipe.get("tpl_id")), None)
            if template is None or template.compute is None:
                raise RecipeComputationError("engineering_recipe_unavailable")
            if not any(recipe.get("params", {}) == params for params in template.params()):
                raise RecipeComputationError("engineering_recipe_parameter_domain")
            try: return template.compute(self.panel, recipe.get("params", {}))
            except Exception as exc: raise RecipeComputationError(type(exc).__name__) from exc
        return self.frame({"kind": "factor", "recipe": recipe}, compute)

    def label(self, hold, phase, end):
        def compute():
            self.label_builds += 1
            return executable_forward_return(self.panel, hold, boundary=end)
        return self.frame({"kind": "label", "hold": hold, "phase": phase, "boundary": end}, compute)


def _evaluate(ctx, candidate, hold, periods):
    factor = ctx.factor(candidate)
    dates = {}
    for phase, bounds in periods.items():
        dates[phase] = list(factor.index[(factor.index >= pd.Timestamp(bounds[0]))
                                        & (factor.index <= pd.Timestamp(bounds[1]))])
    train = ctx.label(hold, "discovery", periods["discovery"][1])
    stats = ic_stats(cross_sectional_ic(factor, train, dates["discovery"]), hold=hold)
    valid = ctx.label(hold, "validation", periods["validation"][1])
    folds = l2_walkforward_forward(factor, valid, dates["validation"],
                                  candidate["expected_direction"], is_ic_mean=stats["ic_mean"], hold=hold)
    return {"state": "computed_engineering", "discovery_ic": stats, "validation": folds,
            "disposition": "evidence_insufficient", "model_admitted": False}


def run_batch(root: str | Path, spec: dict) -> dict:
    """Explicit engineering ZIP->recipes->shared cache->full results/recovery flow.

    Return metadata counts only. Native diagnostics do not certify rights/PIT,
    source qualification, FDR, economic/OOS/forward or strategy admission.
    """
    v = _specification(spec)
    source_identity = _check_sources()
    root = _native_root(root)
    directory = root / "engineering-workbench-v2"
    _unlinked(directory);directory.mkdir(exist_ok=True)
    directory = _native_root(directory)
    cache = directory / "cache"
    _unlinked(cache);cache.mkdir(exist_ok=True)
    binding = {"snapshot": v["snapshot"], "sources": source_identity}
    frozen = _body({"protocol": v, "binding": binding})
    # Same declared protocol may not silently become a new trial after code drift.
    batch = _hash(_body(v).encode())
    dbpath = directory / "batch-journal.sqlite3"
    _unlinked(dbpath)
    with _writer(directory):
        db = sqlite3.connect(dbpath, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL");db.executescript(_DDL)
            with db:
                db.execute("INSERT OR IGNORE INTO batches(id,frozen) VALUES(?,?)", (batch, frozen))
                if db.execute("SELECT frozen FROM batches WHERE id=?", (batch,)).fetchone()[0] != frozen:
                    raise BatchIntegrityError("engineering_batch_frozen_drift")
                db.execute("UPDATE attempts SET state='interrupted',ended=? WHERE batch=? AND state='running'",
                           (datetime.now(timezone.utc).isoformat(), batch))
            panel = load_native_panel_snapshot(root, **v["snapshot"])
            ctx = _Context(panel, db, cache, binding)
            executed = reused = failed = 0
            for hold in v["holding_days"]:
                for c in v["candidates"]:
                    identity = {"candidate": c, "hold": hold, "periods": v["periods"], "binding": binding}
                    prior = db.execute("SELECT result,sha FROM trials WHERE batch=? AND candidate=? AND hold=?",
                                       (batch, c["id"], hold)).fetchone()
                    if prior:
                        _check_sources(binding)
                        if _hash(prior[0].encode()) != prior[1]:
                            raise BatchIntegrityError("engineering_terminal_sha_mismatch")
                        result = json.loads(prior[0])
                        if result.get("trial_identity") != identity:
                            raise BatchIntegrityError("engineering_terminal_identity_mismatch")
                        for sha in result["cache_refs"].values(): ctx.verify(sha)
                        reused += 1
                        failed += result["state"] == "compute_failed"
                        continue
                    _check_sources(binding)
                    with db:
                        cursor = db.execute("INSERT INTO attempts(batch,candidate,hold,state,started) VALUES(?,?,?,'running',?)",
                                            (batch, c["id"], hold, datetime.now(timezone.utc).isoformat()))
                    ctx.accessed = {}
                    try:
                        result = _evaluate(ctx, c, hold, v["periods"])
                    except RecipeComputationError as exc:
                        result = {"state": "compute_failed", "reason": str(exc),
                                  "disposition": "evidence_insufficient", "model_admitted": False}
                        failed += 1
                    result.update(trial_identity=identity, cache_refs=ctx.accessed.copy())
                    encoded = _body(_plain(result))
                    with db:
                        _check_sources(binding)
                        db.execute("INSERT INTO trials(batch,candidate,hold,result,sha) VALUES(?,?,?,?,?)",
                                   (batch, c["id"], hold, encoded, _hash(encoded.encode())))
                        db.execute("UPDATE attempts SET state='terminal',ended=? WHERE id=? AND state='running'",
                                   (datetime.now(timezone.utc).isoformat(), cursor.lastrowid))
                        _check_sources(binding)
                    executed += 1
            _check_sources(binding)
            return {"batch_id": batch, "purpose": "engineering_only", "trials": len(v["candidates"]) * 3,
                    "executed": executed, "terminal_reused": reused, "compute_failed": failed,
                    "factor_builds": ctx.factor_builds, "label_builds": ctx.label_builds,
                    "cache_hits": ctx.cache_hits, "source_qualification_verified": False,
                    "model_admitted": False, "strategy_execution_complete": False}
        finally:
            db.close()


_attest_loaded_disk()
_IMPORTED_SOURCES = _sources()
