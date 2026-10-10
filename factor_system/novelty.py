"""M2.3 新颖性归档：24h 连续探索的「不重复」守卫。

确定性模板网格每轮空转的根因 = 固定种子 + 幂等 dedupe 但 compute 仍全跑。
时间种子的 GP 每轮探索新表达式，但表达式空间有限、会快速重复——
新颖性归档负责：已探索公式不再重算（公式级跳过），与最近探索空间
|IC 相关| ≥ 阈值视为冗余（信号级跳过），只有真正新颖的候选才进入
昂贵的 L0-L3 验证。

持久化：registry/novelty_archive.json（{formula: {seed, first_seen,
fitness, registered, ic_corr_nearest, ic_fingerprint, scope}}）。完整 IC 序列只保留在
当前进程的滚动窗口；紧凑的分箱指纹原子写入 JSON 并全量常驻（每条仅 24 个数），
使探索超过滚动窗口或进程重启后仍能拒绝同一研究作用域内语义上高度相似的候选。
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .metrics import cross_sectional_ic, ic_stats


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


_FINGERPRINT_BINS = 24
_FINGERPRINT_METHOD_VERSION = "binned-mean-24-v1"


def _fingerprint_scope() -> dict:
    """冻结决定 IC 指纹可比性的最小研究口径。"""
    return {
        "research_epoch": config.RESEARCH_EPOCH,
        "discovery_start": config.DISCOVERY_START,
        "discovery_end": config.DISCOVERY_END,
        "hold_days": int(config.HOLD_DAYS),
        "method_version": _FINGERPRINT_METHOD_VERSION,
    }


def _ic_fingerprint(ic: pd.Series) -> list[float] | None:
    """将按时间排列的 IC 压缩为固定长度指纹。

    只压缩已通过最小观测数门槛的序列；固定分箱同时保留了 IC 的
    时间形状，又避免把几百个浮点数写入每条归档。
    """
    values = pd.to_numeric(ic, errors="coerce").to_numpy(dtype=float)
    values = values[np.isfinite(values)]
    if len(values) < 30:
        return None
    chunks = np.array_split(values, _FINGERPRINT_BINS)
    return [round(float(chunk.mean()), 8) for chunk in chunks]


def _fingerprint_corr(left: list[float], right: list[float]) -> float | None:
    """返回两个等长指纹的绝对 Pearson 相关；无法判定时返回 None。"""
    if len(left) != len(right) or len(left) < 3:
        return None
    a = np.asarray(left, dtype=float)
    b = np.asarray(right, dtype=float)
    if not np.isfinite(a).all() or not np.isfinite(b).all():
        return None
    a_centered = a - a.mean()
    b_centered = b - b.mean()
    denom = float(np.linalg.norm(a_centered) * np.linalg.norm(b_centered))
    if denom <= 1e-15:
        return 1.0 if np.allclose(a, b, rtol=1e-7, atol=1e-10) else 0.0
    return abs(float(np.dot(a_centered, b_centered) / denom))


class NoveltyArchive:
    """公式级 + 信号级两层新颖性守卫。"""

    def __init__(self, path=None, window: int = None, threshold: float = None):
        self.path = Path(path) if path is not None else config.REGISTRY_DIR / "novelty_archive.json"
        self.window = window if window is not None else config.DAEMON_NOVELTY_WINDOW
        self.threshold = threshold if threshold is not None else config.DAEMON_NOVELTY_CORR
        self.scope = _fingerprint_scope()
        self._data = self._load()          # {formula: dict}
        self._ic_lib: dict[str, pd.Series] = {}   # 滚动最近探索因子的 IC 序列
        self._fingerprints: dict[str, list[float]] = {}
        self._unverifiable_history: set[str] = set()
        self._order: list[str] = []
        self._restore_fingerprints()
        self._known_formulas: set[str] | None = None

    # ── 持久化 ──
    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
            except Exception as exc:
                raise RuntimeError(f"新颖性归档无法读取，为防重复探索已停止: {self.path}") from exc
            if not isinstance(data, dict):
                raise RuntimeError(f"新颖性归档根结构非对象，已停止: {self.path}")
            if any(not isinstance(formula, str) or not isinstance(rec, dict)
                   for formula, rec in data.items()):
                raise RuntimeError(f"新颖性归档记录结构损坏，已停止: {self.path}")
            return data
        return {}

    def _save(self):
        """同目录原子替换归档；任何写入失败都向上抛出（fail-closed）。"""
        tmp_name = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp_name = tempfile.mkstemp(
                prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
            tmp_name = None
        except Exception as exc:
            raise RuntimeError(f"新颖性归档写入失败，为防去重失效已停止: {self.path}") from exc
        finally:
            if tmp_name is not None:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def _restore_fingerprints(self):
        """只恢复同一研究作用域的指纹；公式记录仍跨作用域永久保留。"""
        found = []
        for formula, rec in self._data.items():
            if not isinstance(rec, dict):
                continue
            raw = rec.get("ic_fingerprint")
            if raw is None:  # 兼容尚未生成指纹的旧归档。
                self._unverifiable_history.add(formula)
                continue
            if not isinstance(raw, list) or len(raw) != _FINGERPRINT_BINS:
                raise RuntimeError(f"新颖性 IC 指纹损坏，已停止: {formula}")
            try:
                fingerprint = [float(value) for value in raw]
            except (TypeError, ValueError):
                raise RuntimeError(f"新颖性 IC 指纹非数值，已停止: {formula}")
            if not np.isfinite(fingerprint).all():
                raise RuntimeError(f"新颖性 IC 指纹含非有限值，已停止: {formula}")
            scope = rec.get("scope")
            if scope is None:  # 旧归档仍参与公式级去重，但不能跨口径参与信号相关。
                self._unverifiable_history.add(formula)
                continue
            if (not isinstance(scope, dict)
                    or set(self.scope) - set(scope)
                    or not isinstance(scope.get("research_epoch"), str)
                    or not isinstance(scope.get("discovery_start"), str)
                    or not isinstance(scope.get("discovery_end"), str)
                    or isinstance(scope.get("hold_days"), bool)
                    or not isinstance(scope.get("hold_days"), int)
                    or not isinstance(scope.get("method_version"), str)):
                raise RuntimeError(f"新颖性 IC 指纹作用域损坏，已停止: {formula}")
            if any(scope.get(key) != value for key, value in self.scope.items()):
                continue
            timestamp = rec.get("updated_at") or rec.get("first_seen") or ""
            found.append((str(timestamp), formula, fingerprint))
        for _timestamp, formula, fingerprint in sorted(found):
            self._fingerprints[formula] = fingerprint

    # ── 公式级（免重算）──
    def seen(self, formula: str) -> bool:
        return formula in self.formulas()

    def formulas(self) -> set[str]:
        """历史原公式 + 配方节点规范化公式，用于搜索前免评估去重。"""
        if self._known_formulas is not None:
            return set(self._known_formulas)
        known = set(self._data)
        try:
            from .search import _canonical_tree, _tree_str
            for path in config.REGISTRY_DIR.joinpath("recipes").glob("*.json"):
                recipe = json.loads(path.read_text("utf-8"))
                nodes = recipe.get("nodes") if isinstance(recipe, dict) else None
                if nodes:
                    canonical = _canonical_tree(nodes)
                    if canonical is not None:
                        known.add(_tree_str(canonical))
        except Exception:
            pass
        self._known_formulas = known
        return set(known)

    # ── 信号级（免重复验证）──
    def check_novelty(self, formula: str, factor: pd.DataFrame,
                      is_dates: list, fwd: pd.DataFrame) -> dict:
        """与滚动库最近因子 |IC 相关| < 阈值 → novel。

        返回 {novel, reason, ic_corr_nearest, nearest, ic_ir}。IC 不足 → 非新颖（诚实拒）。
        """
        if self._unverifiable_history:
            return {
                "novel": False,
                "reason": "历史信号指纹缺失，无法安全判定新颖性",
                "ic_corr_nearest": None,
                "nearest": None,
                "ic_ir": None,
                "ic": None,
                "unverifiable_history_count": len(self._unverifiable_history),
            }
        ic = cross_sectional_ic(factor, fwd, is_dates)
        if ic is None or ic.notna().sum() < 30:
            return {"novel": False, "reason": "IC 观测不足",
                    "ic_corr_nearest": None, "nearest": None, "ic_ir": None, "ic": None}
        fingerprint = _ic_fingerprint(ic)
        best_corr, best_f = 0.0, None
        for f2, ic2 in self._ic_lib.items():
            common = ic.index.intersection(ic2.index)
            if len(common) < 30:
                continue
            c = ic.loc[common].corr(ic2.loc[common])
            if not pd.notna(c):
                continue
            if abs(c) > best_corr:
                best_corr, best_f = abs(c), f2
        if fingerprint is not None:
            for f2, archived in self._fingerprints.items():
                c = _fingerprint_corr(fingerprint, archived)
                if c is not None and c > best_corr:
                    best_corr, best_f = c, f2
        st = ic_stats(ic)
        if best_f is None:
            # 当前完整 IC 库与持久指纹库都为空（第一轮）。
            return {"novel": True, "reason": "滚动库为空", "ic_corr_nearest": 0.0,
                    "nearest": None, "ic_ir": st["ic_ir"], "ic": ic}
        novel = bool(best_corr < self.threshold)
        return {"novel": novel,
                "reason": "与最近探索空间相关过高(冗余)" if not novel else "新颖",
                "ic_corr_nearest": round(best_corr, 3), "nearest": best_f,
                "ic_ir": st["ic_ir"], "ic": ic}

    # ── 记录 + 滚动库 ──
    def record(self, formula: str, *, registered: bool = False,
               fitness: float = None, ic_corr_nearest: float = None,
               seed: int = None):
        existed = formula in self._data
        previous = self._data.get(formula)
        rec = dict(previous) if isinstance(previous, dict) else {"first_seen": _now()}
        rec.update({
            "registered": registered,
            "updated_at": _now(),
        })
        if fitness is not None:
            rec["fitness"] = round(float(fitness), 4) if pd.notna(fitness) else None
        if ic_corr_nearest is not None:
            rec["ic_corr_nearest"] = ic_corr_nearest
        if seed is not None:
            rec["seed"] = seed
        self._data[formula] = rec
        try:
            self._save()
        except Exception:
            if existed:
                self._data[formula] = previous
            else:
                self._data.pop(formula, None)
            raise
        if self._known_formulas is not None:
            self._known_formulas.add(formula)

    def push_ic(self, formula: str, ic: pd.Series):
        """把 IC 推进滚动库，并先原子持久化紧凑指纹。"""
        if formula in self._ic_lib or formula in self._fingerprints:
            return
        fingerprint = _ic_fingerprint(ic)
        if fingerprint is None:
            raise ValueError("持久化 IC 指纹至少需要 30 个有效观测")

        existed = formula in self._data
        previous = self._data.get(formula)
        rec = dict(previous) if isinstance(previous, dict) else {"first_seen": _now()}
        rec.update({
            "registered": bool(rec.get("registered", False)),
            "updated_at": _now(),
            "ic_fingerprint": fingerprint,
            "scope": dict(self.scope),
        })
        self._data[formula] = rec
        try:
            self._save()
        except Exception:
            if existed:
                self._data[formula] = previous
            else:
                self._data.pop(formula, None)
            raise

        self._ic_lib[formula] = ic
        self._fingerprints[formula] = fingerprint
        self._order.append(formula)
        if len(self._order) > self.window:
            old = self._order.pop(0)
            self._ic_lib.pop(old, None)

    # ── 统计 ──
    def count_novel_today(self) -> int:
        today = _now()[:10]
        return sum(1 for r in self._data.values() if (r.get("first_seen") or "")[:10] == today)

    def total_explored(self) -> int:
        return len(self._data)

    def registered_count(self) -> int:
        return sum(1 for r in self._data.values() if r.get("registered"))


__all__ = ["NoveltyArchive"]
