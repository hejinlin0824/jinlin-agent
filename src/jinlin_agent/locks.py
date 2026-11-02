"""Cross-platform advisory file locks.

The lock pathname is a persistent rendezvous point: it is never unlinked on
release, because unlinking a locked file can split contenders across different
inodes. POSIX opens reject symlinks where O_NOFOLLOW is available, and every
opened lock object is checked to be a regular file before it is locked.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path


class SessionLockedError(RuntimeError):
    pass


class SessionLock:
    """Cross-platform advisory lock (msvcrt on Windows, fcntl elsewhere).

    Locks are process-scoped by the OS and are released automatically if the
    process exits, including abrupt termination. The lock file itself remains
    in place so all contenders continue to coordinate on the same inode.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self, blocking: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
        # Do not follow a lock-file symlink on platforms that expose this flag.
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.path, flags, 0o600)
        except OSError as exc:
            raise SessionLockedError(
                f"cannot safely open session lock: {self.path}: {exc}"
            ) from exc

        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise SessionLockedError(
                    f"session lock is not a regular file: {self.path}"
                )
            self._fh = os.fdopen(fd, "r+b", buffering=0)
            fd = -1
            if sys.platform == "win32":
                # msvcrt.locking locks a byte range. Ensure byte zero exists
                # before attempting to lock it; concurrent initializers write
                # the same byte and do not change the rendezvous inode.
                if os.fstat(self._fh.fileno()).st_size == 0:
                    self._fh.seek(0)
                    self._fh.write(b"\0")
                    self._fh.flush()
                self._fh.seek(0)
                import msvcrt

                mode = msvcrt.LK_NBLCK if not blocking else msvcrt.LK_LOCK
                msvcrt.locking(self._fh.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(self._fh.fileno(), flags)
        except SessionLockedError:
            self.release()
            if fd >= 0:
                os.close(fd)
            raise
        except OSError as exc:
            self.release()
            if fd >= 0:
                os.close(fd)
            raise SessionLockedError(
                f"session is locked by another process or cannot be locked: {self.path}"
            ) from exc

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "SessionLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()
