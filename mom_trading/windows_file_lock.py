"""The flock subset used by this app, backed by real Windows kernel locks.

The marker is beyond EOF so reading lock-file diagnostics stays possible, as
with POSIX advisory locks. Cooperating writers/readers must all use flock.
No claim is made about arbitrary writers, distributed locks or Linux testing.
"""
from __future__ import annotations

import os
import sys
import types


def install() -> None:
    if os.name != "nt":
        return
    existing = sys.modules.get("fcntl")
    if getattr(existing, "_regis_kernel_lock", False):
        return
    import ctypes
    from ctypes import wintypes
    import errno
    import msvcrt

    class Overlapped(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    lock = kernel.LockFileEx
    lock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                     wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(Overlapped)]
    lock.restype = wintypes.BOOL
    unlock = kernel.UnlockFileEx
    unlock.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD,
                       wintypes.DWORD, ctypes.POINTER(Overlapped)]
    unlock.restype = wintypes.BOOL

    def flock(fd: int, operation: int) -> None:
        handle = msvcrt.get_osfhandle(fd)
        marker = Overlapped(Offset=0xFFFFFFFF, OffsetHigh=0x7FFFFFFF)
        if operation == 8:
            if not unlock(handle, 0, 1, 0, ctypes.byref(marker)):
                error = ctypes.get_last_error()
                if error != 158:  # ERROR_NOT_LOCKED; unlock is harmless if not held.
                    raise ctypes.WinError(error)
            return
        if operation not in (1, 2, 5, 6):
            raise ValueError("Only LOCK_SH/LOCK_EX with optional LOCK_NB are supported")
        flags = (2 if operation & 2 else 0) | (1 if operation & 4 else 0)
        if not lock(handle, flags, 0, 1, 0, ctypes.byref(marker)):
            error = ctypes.get_last_error()
            if error == 33 and operation & 4:
                raise BlockingIOError(errno.EWOULDBLOCK, "File lock is held")
            raise ctypes.WinError(error)

    module = types.ModuleType("fcntl")
    module.LOCK_SH, module.LOCK_EX, module.LOCK_NB, module.LOCK_UN = 1, 2, 4, 8
    module.flock = flock
    module._regis_kernel_lock = True
    module.__doc__ = __doc__
    sys.modules["fcntl"] = module
