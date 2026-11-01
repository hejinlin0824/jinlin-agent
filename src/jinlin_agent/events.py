"""Append-only event log: the single source of truth.

Guarantees:
  * one line per event, UTF-8 JSON, `\\n` terminated
  * `seq` strictly increasing per session
  * every append is fsync'ed before the caller proceeds (side effects happen
    only after the intent event is durable)
  * exactly one writable process per session (advisory file lock)
  * a torn trailing line after a crash is ignored on read, never fatal
  * `EventLog.open()` repairs a tolerated torn tail *before* appending: an
    unparseable unterminated fragment is truncated away, a complete final
    event missing its newline gets the terminator back, and a
    newline-terminated corrupt line still fails closed (SESSION_CORRUPTED)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Iterator

from .domain import new_id, utcnow_iso
from .locks import SessionLock, SessionLockedError  # noqa: F401  (re-exported)

TornLineError = None  # marker for tooling


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
        try:
            if acquire_lock:
                self._lock.acquire()
            self._repair_tail_before_append()
            self._fh = open(self.path, "ab")
            return self
        except BaseException:
            # never leak the session lock because the log failed validation
            if acquire_lock:
                self._lock.release()
            raise

    def _repair_tail_before_append(self) -> None:
        """Make the file safe to append to, under the reader's tolerance rules.

        * torn fragment (unparseable, no trailing newline): truncate to the
          last complete event, fsync;
        * complete event missing its newline: write the terminator, fsync;
        * newline-terminated corruption: not repaired — `_parse_log_data`
          already raised SESSION_CORRUPTED (fail closed).
        """
        if not self.path.exists():
            return
        data = self.path.read_bytes()
        if not data:
            return
        events, tail = _parse_log_data(data)
        for evt in events:
            if isinstance(evt.get("seq"), int):
                self._last_seq = max(self._last_seq, evt["seq"])
        if tail == _TAIL_TORN:
            good_end = data.rfind(b"\n") + 1
            with open(self.path, "r+b") as fh:
                fh.truncate(good_end)
                fh.flush()
                os.fsync(fh.fileno())
        elif tail == _TAIL_MISSING_NEWLINE:
            with open(self.path, "ab") as fh:
                fh.write(b"\n")
                fh.flush()
                os.fsync(fh.fileno())

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


# tail verdicts returned by _parse_log_data
_TAIL_CLEAN = "clean"  # empty file, or last line newline-terminated
_TAIL_MISSING_NEWLINE = "no_newline"  # last line is a fully valid event, missing \n
_TAIL_TORN = "torn"  # trailing unterminated fragment that never parsed


def _parse_log_data(data: bytes) -> tuple[list[dict[str, Any]], str]:
    """Parse raw event-log bytes into validated events plus a tail verdict.

    Raises SESSION_CORRUPTED for anything the reader must reject: a
    newline-terminated line that fails to parse, a blank line, a schema /
    session mismatch, or a sequence gap. The only tolerated damage is a
    trailing fragment **without** a newline that fails to parse (a genuine
    torn write), reported as ``_TAIL_TORN``; a trailing line that parses and
    passes every check but lacks its newline is reported as
    ``_TAIL_MISSING_NEWLINE`` so the writer can repair it before appending.
    """
    from .errors import SESSION_CORRUPTED, AgentError

    if not data:
        return [], _TAIL_CLEAN
    ends_with_newline = data.endswith(b"\n")
    lines = data.split(b"\n")
    if ends_with_newline:
        lines = lines[:-1]
    events: list[dict[str, Any]] = []
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
                return events, _TAIL_TORN  # genuine torn tail: incomplete final write
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
        events.append(evt)
    return events, (_TAIL_CLEAN if ends_with_newline else _TAIL_MISSING_NEWLINE)


def _iter_events_checked(path: Path) -> Iterator[dict[str, Any]]:
    """Replay an event log.

    Tolerance is deliberately narrow: only a final fragment **without a trailing
    newline** may be a torn write from a crash. A newline-terminated line that
    fails to parse, a blank line between events, a sequence gap, or a schema /
    session mismatch all raise SESSION_CORRUPTED. (The tail verdict that tells
    the writer how to repair the file is computed by `_parse_log_data`; reading
    simply drops torn tails.)
    """
    from .errors import SESSION_CORRUPTED  # noqa: F401  (kept for API parity)

    path = Path(path)
    if not path.exists():
        return
    data = path.read_bytes()
    events, _tail = _parse_log_data(data)
    yield from events


def read_events(session_dir: Path) -> Iterator[dict[str, Any]]:
    """Read events without taking the write lock (trace/stats/recovery viewer)."""
    yield from _iter_events_checked(Path(session_dir) / "events.jsonl")


def new_session_id() -> str:
    return f"sess_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
