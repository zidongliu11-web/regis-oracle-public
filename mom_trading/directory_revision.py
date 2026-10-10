"""Bounded, fail-closed revision for a large, flat Linux source directory.

The first call records physical file stamps after installing an inotify watch.
Subsequent calls drain directory events instead of stat-ing every member on an
HTTP request. An invalid watch or overflowing event queue is never treated as
an unchanged input. Other platforms retain the caller's full-scan fallback.
"""

from collections import OrderedDict
import ctypes
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import threading


_EVENT = struct.Struct("iIII")
_CHANGE_MASK = 0x00000002 | 0x00000004 | 0x00000008 | 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200
_LOST_MASK = 0x00000400 | 0x00000800 | 0x00002000 | 0x00004000 | 0x00008000
_ONLY_DIR = 0x01000000
_WATCHES: OrderedDict[str, "_Watch"] = OrderedDict()
_LOCK = threading.RLock()
_MAX_WATCHES = 8


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode)


class _Watch:
    def __init__(self, directory: Path):
        self.directory = directory
        self.fd = -1
        self.generation = 0
        self.invalid = False
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        add = libc.inotify_add_watch
        add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        add.restype = ctypes.c_int
        fd = init(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1_failed")
        self.fd = fd
        try:
            if add(fd, os.fsencode(directory), _CHANGE_MASK | _ONLY_DIR) < 0:
                raise OSError(ctypes.get_errno(), "inotify_add_watch_failed")
            info = directory.stat()
            self.directory_identity = info.st_dev, info.st_ino
            digest = hashlib.sha256()
            with os.scandir(directory) as entries:
                ordered = sorted(entries, key=lambda entry: entry.name)
            for entry in ordered:
                try:
                    stamp = _stamp(entry.stat(follow_symlinks=False))
                except FileNotFoundError:
                    stamp = None
                digest.update(json.dumps((entry.name, stamp), ensure_ascii=True,
                    separators=(",", ":")).encode("utf-8"))
                digest.update(b"\n")
            self.baseline = digest.hexdigest()
            self._drain()
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def _drain(self):
        seen = 0
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            if not data:
                self.invalid = True
                break
            seen += len(data)
            if seen > 1024 * 1024:
                self.invalid = True
                break
            offset = 0
            while offset + _EVENT.size <= len(data):
                _, mask, _, name_len = _EVENT.unpack_from(data, offset)
                offset += _EVENT.size + name_len
                if mask & _LOST_MASK:
                    self.invalid = True
                elif mask & _CHANGE_MASK:
                    self.generation += 1
            if offset != len(data):
                self.invalid = True

    def revision(self):
        info = self.directory.stat()
        if (self.invalid or self.fd < 0
                or (info.st_dev, info.st_ino) != self.directory_identity):
            raise OSError("directory_watch_invalid")
        self._drain()
        if self.invalid:
            raise OSError("directory_watch_events_lost")
        return self.baseline, self.generation


def revision(directory: Path):
    """Return an exact change generation, or None for the full-scan fallback."""
    if sys.platform != "linux" or not directory.is_dir():
        return None
    key = str(directory.absolute())
    with _LOCK:
        watch = _WATCHES.get(key)
        if watch is None:
            watch = _Watch(directory)
            _WATCHES[key] = watch
            if len(_WATCHES) > _MAX_WATCHES:
                _, oldest = _WATCHES.popitem(last=False)
                oldest.close()
        else:
            _WATCHES.move_to_end(key)
        try:
            return watch.revision()
        except OSError:
            _WATCHES.pop(key, None)
            watch.close()
            raise
