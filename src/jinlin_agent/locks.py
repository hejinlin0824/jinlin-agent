"""Cross-platform advisory file locks.

Shared by the event log (one writer per session) and the editing tools (one
write phase per workspace), so both serialize through the same primitive
without tools depending on events.
"""

from __future__ import annotations

import sys
from pathlib import Path


class SessionLockedError(RuntimeError):
    pass


class SessionLock:
    """Cross-platform advisory lock (msvcrt on Windows, fcntl elsewhere)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh = None

    def acquire(self, blocking: bool = False) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                mode = msvcrt.LK_NBLCK if not blocking else msvcrt.LK_LOCK
                msvcrt.locking(self._fh.fileno(), mode, 1)
            else:
                import fcntl

                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(self._fh.fileno(), flags)
        except OSError as exc:  # lock already held
            self._fh.close()
            self._fh = None
            raise SessionLockedError(
                f"session is locked by another process: {self.path}"
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
