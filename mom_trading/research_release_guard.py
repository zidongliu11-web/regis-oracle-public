"""Read leases keep a running job on one source version during publication."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import stat
import time

DIRECTORY = Path("/var/lib/regis-oracle-release")
OWNER_UID = 0
REQUEST_TTL_SECONDS = 180


def protected(info, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if info.st_uid != OWNER_UID or info.st_mode & 0o022 or not kind(info.st_mode):
        raise RuntimeError("research_release_guard_not_protected")


def publication_requested():
    try:
        info = (DIRECTORY / "requested").lstat()
    except FileNotFoundError:
        return False
    protected(info)
    # An abandoned publisher cannot indefinitely stop new research jobs.
    return -5 <= time.time() - info.st_mtime <= REQUEST_TTL_SECONDS


@contextmanager
def source_lease(*, required=False):
    """None means defer; descriptors must also be inherited by a spawned child."""
    # Remote web processes do not inherit the research worker's role setting.
    # Their callers explicitly require the same administrative read lock.
    if not required and os.environ.get("REGIS_EXECUTION_ROLE") != "cloud":
        yield ()
        return
    protected(DIRECTORY.lstat(), directory=True)
    fd = os.open(DIRECTORY / "readers.lock", os.O_RDONLY | os.O_NOFOLLOW)
    try:
        protected(os.fstat(fd))
        ready = False
        if not publication_requested():
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                ready = not publication_requested()
            except BlockingIOError:
                pass
        yield (fd,) if ready else None
    finally:
        os.close(fd)
