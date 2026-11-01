"""Append-only event log: the single source of truth.

Guarantees:
  * one line per event, UTF-8 JSON, `\\n` terminated
  * `seq` strictly increasing per session
  * every append is fsync'ed before the caller proceeds (side effects happen
    only after the intent event is durable)
  * exactly one writable process per session (advisory file lock)
  * a torn trailing line after a crash is ignored on read, never fatal
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from .domain import new_id, utcnow_iso

TornLineError = None  # marker for tooling


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


class EventLog:
    SCHEMA_VERSION = 1

    def __init__(self, session_dir: Path, session_id: str) -> None:
        self.session_dir = Path(session_dir)
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.session_dir / "events.jsonl"
        self.session_id = session_id
        self._last_seq = 0
        self._lock = SessionLock(self.session_dir / "session.lock")
        self._fh = None

    # ------------------------------------------------------------------ open

    def open(self, acquire_lock: bool = True) -> "EventLog":
        if acquire_lock:
            self._lock.acquire()
        if self.path.exists():
            for evt in self.iter_events():
                if isinstance(evt.get("seq"), int):
                    self._last_seq = max(self._last_seq, evt["seq"])
        self._fh = open(self.path, "ab")
        return self

    def close(self) -> None:
        if self._fh:
            self._fh.close()
            self._fh = None
        self._lock.release()

    def __enter__(self) -> "EventLog":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ write

    @property
    def last_seq(self) -> int:
        return self._last_seq

    def append(
        self,
        type: str,
        payload: dict[str, Any] | None = None,
        turn_id: str = "",
        step_id: str = "",
    ) -> dict[str, Any]:
        if self._fh is None:
            raise RuntimeError("EventLog.open() must be called before append()")
        self._last_seq += 1
        event = {
            "schema_version": self.SCHEMA_VERSION,
            "event_id": new_id("evt"),
            "seq": self._last_seq,
            "ts": utcnow_iso(),
            "session_id": self.session_id,
            "turn_id": turn_id,
            "step_id": step_id,
            "type": type,
            "payload": payload or {},
        }
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        self._fh.write(line.encode("utf-8") + b"\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())
        return event

    # ------------------------------------------------------------------ read

    def iter_events(self) -> Iterator[dict[str, Any]]:
        yield from _iter_events_checked(self.path)


def _iter_events_checked(path: Path) -> Iterator[dict[str, Any]]:
    """Replay an event log; tolerate only a torn tail, never mid-log corruption."""
    from .errors import SESSION_CORRUPTED, AgentError

    if not Path(path).exists():
        return
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        lines = fh.read().split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    expected = 1
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            evt = json.loads(stripped)
        except json.JSONDecodeError as exc:
            if index == len(lines) - 1:
                # a torn write after a crash may leave the final line incomplete
                break
            raise AgentError(
                SESSION_CORRUPTED,
                f"corrupt event at line {index + 1} (mid-log corruption is not ignored)",
                source="events",
                details={"line": index + 1, "error": str(exc)},
            ) from exc
        if not isinstance(evt, dict) or "type" not in evt or not isinstance(evt.get("seq"), int):
            raise AgentError(
                SESSION_CORRUPTED,
                f"invalid event at line {index + 1}: missing type/seq",
                source="events",
            )
        if evt["seq"] != expected:
            raise AgentError(
                SESSION_CORRUPTED,
                f"event sequence gap at line {index + 1}: expected seq {expected}, found {evt['seq']}",
                source="events",
            )
        expected = evt["seq"] + 1
        yield evt


def read_events(session_dir: Path) -> Iterator[dict[str, Any]]:
    """Read events without taking the write lock (trace/stats/recovery viewer)."""
    yield from _iter_events_checked(Path(session_dir) / "events.jsonl")


def new_session_id() -> str:
    return f"sess_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
