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
    """Replay an event log.

    Tolerance is deliberately narrow: only a final fragment **without a trailing
    newline** may be a torn write from a crash. A newline-terminated line that
    fails to parse, a blank line between events, a sequence gap, or a schema /
    session mismatch all raise SESSION_CORRUPTED.
    """
    from .errors import SESSION_CORRUPTED, AgentError

    path = Path(path)
    if not path.exists():
        return
    data = path.read_bytes()
    if not data:
        return
    ends_with_newline = data.endswith(b"\n")
    lines = data.split(b"\n")
    if ends_with_newline:
        lines = lines[:-1]
    expected = 1
    session_id: str | None = None
    for index, raw in enumerate(lines):
        line_no = index + 1
        is_last = index == len(lines) - 1
        if not raw.strip():
            raise AgentError(
                SESSION_CORRUPTED,
                f"blank line at line {line_no}; event log must be a dense sequence of JSON lines",
                source="events",
            )
        try:
            evt = json.loads(raw.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if is_last and not ends_with_newline:
                break  # genuine torn tail: incomplete final write
            raise AgentError(
                SESSION_CORRUPTED,
                f"corrupt event at line {line_no} (a newline-terminated line must parse)",
                source="events",
                details={"line": line_no, "error": str(exc), "torn_tail_allowed": False},
            ) from exc
        if not isinstance(evt, dict):
            raise AgentError(SESSION_CORRUPTED, f"invalid event at line {line_no}: not an object", source="events")
        if evt.get("schema_version") != EventLog.SCHEMA_VERSION:
            raise AgentError(
                SESSION_CORRUPTED,
                f"unsupported schema_version at line {line_no}: {evt.get('schema_version')!r}",
                source="events",
            )
        if not isinstance(evt.get("type"), str) or not isinstance(evt.get("seq"), int):
            raise AgentError(SESSION_CORRUPTED, f"invalid event at line {line_no}: missing type/seq", source="events")
        if not isinstance(evt.get("payload", {}), dict):
            raise AgentError(SESSION_CORRUPTED, f"invalid event at line {line_no}: payload must be an object", source="events")
        sid = evt.get("session_id")
        if sid is not None:
            if not isinstance(sid, str):
                raise AgentError(SESSION_CORRUPTED, f"invalid session_id at line {line_no}", source="events")
            if session_id is None:
                session_id = sid
            elif sid != session_id:
                raise AgentError(
                    SESSION_CORRUPTED,
                    f"session_id mismatch at line {line_no}: {sid!r} != {session_id!r}",
                    source="events",
                )
        if evt["seq"] != expected:
            raise AgentError(
                SESSION_CORRUPTED,
                f"event sequence gap at line {line_no}: expected seq {expected}, found {evt['seq']}",
                source="events",
            )
        expected = evt["seq"] + 1
        yield evt


def read_events(session_dir: Path) -> Iterator[dict[str, Any]]:
    """Read events without taking the write lock (trace/stats/recovery viewer)."""
    yield from _iter_events_checked(Path(session_dir) / "events.jsonl")


def new_session_id() -> str:
    return f"sess_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
