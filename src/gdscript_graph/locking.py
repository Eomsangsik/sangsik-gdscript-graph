from __future__ import annotations

import os
import time
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl

_WINDOWS_RETRY_SECONDS = 0.1


class FileLock:
    """An exclusive inter-process lock on a lock file -- `flock` on POSIX,
    `msvcrt.locking` on Windows. Held by an open file handle, so the OS drops
    it when the holder exits, crashes included. Two `FileLock`s on the same
    path exclude each other even within one process."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def acquire(self, blocking: bool = True) -> bool:
        if self._file is not None:
            raise RuntimeError(f"lock already held: {self.path}")
        f = open(self.path, "a+b")
        try:
            if _try_lock(f, blocking):
                self._file = f
                return True
        except BaseException:
            f.close()
            raise
        f.close()
        return False

    def release(self) -> None:
        if self._file is None:
            return
        try:
            _unlock(self._file)
        finally:
            self._file.close()
            self._file = None

    def is_held_elsewhere(self) -> bool:
        """Whether some other holder has the lock right now (checked by
        briefly taking it)."""
        if self._file is not None:
            return False
        if self.acquire(blocking=False):
            self.release()
            return False
        return True

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def _try_lock(f, blocking: bool) -> bool:
    if os.name == "nt":
        while True:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                if not blocking:
                    return False
                time.sleep(_WINDOWS_RETRY_SECONDS)
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False


def _unlock(f) -> None:
    if os.name == "nt":
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def build_lock(db_path: Path) -> FileLock:
    """Held for the whole of a build of `db_path`, by every process."""
    return FileLock(db_path.with_name(f"{db_path.name}.lock"))


def watch_lock(db_path: Path) -> FileLock:
    """Held by the one server process that watches the project and rebuilds
    `db_path` -- see `watch.start_watching`."""
    return FileLock(db_path.with_name(f"{db_path.name}.watch.lock"))
