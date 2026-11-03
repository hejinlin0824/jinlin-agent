"""Task Ledger (v2.0 WP-V2.0-1; ADR-003, ADR-008).

The sqlite database is the task-layer *projection*: an index of tasks, worker
attempts and protocol messages. Every write FIRST appends an event to the root
task's event log (`<home>/tasks/<root_task_id>/events.jsonl`) — which reuses
the battle-tested append-only log (fsync, seq checks, torn-tail repair,
single-writer lock) — and only then commits the row. If the process dies in
between, `rebuild()` re-derives the projection from the logs, so the ledger
stays reconstructable: same discipline as state.py's session fold, with the
log as truth and the index as convenience.

Concurrency (WP-1 scope): one writer per process (RLock-guarded methods);
sqlite's own file locking covers cross-process access; a short-lived init
lock keeps two processes from racing the schema creation. The connection
stays on its creating thread (default check_same_thread); WP-2 revisits this
when workers go multithreaded.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .errors import (
    LEDGER_UNAVAILABLE,
    SESSION_CORRUPTED,
    TASK_ALREADY_EXISTS,
    TASK_MESSAGE_INVALID,
    TASK_NOT_FOUND,
    TASK_RESULT_INVALID,
    TASK_TRANSITION_INVALID,
    AgentError,
)
from .events import EventLog, read_events
from .locks import SessionLock, SessionLockedError
from .taskmodel import (
    TaskMessage,
    TaskSpec,
    WorkerRecord,
    can_transition,
    validate_task_result,
)

_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    root_task_id TEXT NOT NULL,
    parent_task_id TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    task_kind TEXT NOT NULL DEFAULT 'implementation',
    status TEXT NOT NULL DEFAULT 'created',
    role TEXT NOT NULL DEFAULT 'worker',
    dependencies TEXT NOT NULL DEFAULT '[]',
    capabilities TEXT NOT NULL DEFAULT '[]',
    workspace_scope TEXT NOT NULL DEFAULT '{}',
    acceptance_criteria TEXT NOT NULL DEFAULT '[]',
    budget TEXT NOT NULL DEFAULT '{}',
    verification TEXT NOT NULL DEFAULT 'code',
    requires_review INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS workers (
    worker_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    root_task_id TEXT NOT NULL DEFAULT '',
    role_id TEXT NOT NULL DEFAULT 'worker',
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'created',
    session_id TEXT NOT NULL DEFAULT '',
    workspace_path TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    base_commit TEXT NOT NULL DEFAULT '',
    budget_used TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    root_task_id TEXT NOT NULL DEFAULT '',
    task_id TEXT NOT NULL DEFAULT '',
    worker_id TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 0,
    message_type TEXT NOT NULL DEFAULT '',
    correlation_id TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT '',
    recorded_at TEXT NOT NULL DEFAULT '',
    processed_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_tasks_root ON tasks (root_task_id);
CREATE INDEX IF NOT EXISTS idx_workers_task ON workers (task_id);
CREATE INDEX IF NOT EXISTS idx_messages_root ON messages (root_task_id);
"""

_TASK_COLS = (
    "task_id, root_task_id, parent_task_id, title, description, task_kind, status, role, "
    "dependencies, capabilities, workspace_scope, acceptance_criteria, budget, verification, "
    "requires_review, result, error, created_at, updated_at"
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class TaskLedger:
    SCHEMA_VERSION = 2

    def __init__(self, config: Any) -> None:
        self.config = config
        self.path = Path(config.ledger_path)
        self._conn: sqlite3.Connection | None = None
        self._mu = threading.RLock()
        self._logs: dict[str, EventLog] = {}
        self._init_lock_handle: SessionLock | None = None

    # ------------------------------------------------------------------ lifecycle

    def open(self) -> "TaskLedger":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config.tasks_dir.mkdir(parents=True, exist_ok=True)
        self._acquire_init_lock()
        try:
            # One shared connection for the process (WP-V2.0-2 decision, revisiting
            # the WP-1 note): check_same_thread=False + every operation under
            # self._mu makes the ledger safe for the worker runtime's threads.
            self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._ensure_schema()
        except BaseException:
            self._release_init_lock()
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            raise
        self._release_init_lock()
        return self

    def close(self) -> None:
        with self._mu:
            for log in self._logs.values():
                try:
                    log.close()
                except Exception:
                    pass
            self._logs.clear()
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    def __enter__(self) -> "TaskLedger":
        return self.open()

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _acquire_init_lock(self) -> None:
        lock = SessionLock(self.path.parent / "ledger.lock")
        deadline = time.monotonic() + 5.0
        while True:
            try:
                lock.acquire()
                self._init_lock_handle = lock
                return
            except SessionLockedError:
                if time.monotonic() > deadline:
                    raise AgentError(
                        LEDGER_UNAVAILABLE,
                        "another process holds the ledger init lock; retry later",
                        source="ledger",
                    )
                time.sleep(0.2)

    def _release_init_lock(self) -> None:
        if self._init_lock_handle is not None:
            self._init_lock_handle.release()
            self._init_lock_handle = None

    def _ensure_schema(self) -> None:
        conn = self._require_conn()
        conn.execute("PRAGMA journal_mode=WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > self.SCHEMA_VERSION:
            raise AgentError(
                LEDGER_UNAVAILABLE,
                f"ledger schema v{version} is newer than this build supports (v{self.SCHEMA_VERSION}); "
                "upgrade jinlin-agent or restore a compatible ledger",
                source="ledger",
            )
        if version < self.SCHEMA_VERSION:
            if version < 1:
                conn.executescript(_SCHEMA_V2)
            else:
                # v1 -> v2: protocol processing marks + review requirement flag
                conn.execute("ALTER TABLE messages ADD COLUMN processed_at TEXT NOT NULL DEFAULT ''")
                conn.execute("ALTER TABLE tasks ADD COLUMN requires_review INTEGER NOT NULL DEFAULT 0")
            conn.execute(f"PRAGMA user_version = {self.SCHEMA_VERSION}")
            conn.commit()

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("TaskLedger.open() must be called first")
        return self._conn

    # ------------------------------------------------------------------ task event stream

    def _log_for(self, root_task_id: str) -> EventLog:
        log = self._logs.get(root_task_id)
        if log is None:
            log = EventLog(self.config.tasks_dir / root_task_id, root_task_id).open()
            self._logs[root_task_id] = log
        return log

    def _emit(self, root_task_id: str, etype: str, payload: dict[str, Any]) -> None:
        """Event first, commit second: a crash in between leaves an extra event
        that rebuild() folds in — never a row the log cannot account for."""
        self._log_for(root_task_id).append(etype, payload)

    def emit(self, root_task_id: str, etype: str, payload: dict[str, Any]) -> None:
        """Public task-stream event (task-layer decisions such as strategy
        selection and replanning that have no row of their own)."""
        with self._mu:
            self._emit(root_task_id, etype, payload)

    # ------------------------------------------------------------------ tasks

    def create_task(self, spec: TaskSpec) -> dict[str, Any]:
        conn = self._require_conn()
        root = spec.root_task_id or spec.task_id
        with self._mu:
            if self.get_task(spec.task_id) is not None:
                raise AgentError(TASK_ALREADY_EXISTS, f"task already exists: {spec.task_id}", source="ledger")
            data = spec.to_dict()
            self._emit(
                root,
                "task.created",
                {"task": data},
            )
            conn.execute(
                f"INSERT INTO tasks ({_TASK_COLS}) VALUES ({','.join('?' * 19)})",
                (
                    spec.task_id,
                    root,
                    spec.parent_task_id,
                    spec.title,
                    spec.description,
                    spec.task_kind,
                    "created",
                    spec.role,
                    _json(spec.dependencies),
                    _json(spec.capabilities),
                    _json(spec.workspace_scope.to_dict()),
                    _json(spec.acceptance_criteria),
                    _json(spec.budget.to_dict()),
                    spec.verification,
                    1 if spec.requires_review else 0,
                    None,
                    "",
                    spec.created_at,
                    spec.created_at,
                ),
            )
            conn.commit()
            return self.get_task(spec.task_id)  # type: ignore[return-value]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._mu:
            row = self._require_conn().execute(f"SELECT {_TASK_COLS} FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
            return self._task_row(row) if row is not None else None

    def require_task(self, task_id: str) -> dict[str, Any]:
        row = self.get_task(task_id)
        if row is None:
            raise AgentError(TASK_NOT_FOUND, f"unknown task: {task_id}", source="ledger")
        return row

    def list_tasks(self, root_task_id: str | None = None) -> list[dict[str, Any]]:
        with self._mu:
            conn = self._require_conn()
            if root_task_id:
                rows = conn.execute(
                    f"SELECT {_TASK_COLS} FROM tasks WHERE root_task_id = ? ORDER BY created_at DESC, task_id",
                    (root_task_id,),
                ).fetchall()
            else:
                rows = conn.execute(f"SELECT {_TASK_COLS} FROM tasks ORDER BY created_at DESC, task_id").fetchall()
            return [self._task_row(r) for r in rows]

    def set_task_status(self, task_id: str, new_status: str, reason: str = "") -> dict[str, Any]:
        conn = self._require_conn()
        with self._mu:
            row = self.require_task(task_id)
            current = row["status"]
            if not can_transition(current, new_status):
                raise AgentError(
                    TASK_TRANSITION_INVALID,
                    f"illegal task transition {current} -> {new_status} for {task_id}"
                    + (f"; terminal states are never revived" if current in ("completed", "failed", "cancelled") else ""),
                    source="ledger",
                )
            self._emit(task_id_root(row), "task.status_changed", {"task_id": task_id, "from": current, "to": new_status, "reason": reason})
            conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE task_id = ?",
                (new_status, _now(), task_id),
            )
            conn.commit()
            return self.get_task(task_id)  # type: ignore[return-value]

    def record_result(self, task_id: str, result: dict[str, Any]) -> dict[str, Any]:
        """Store the structured worker deliverable (v2 §7.4) on its task."""
        conn = self._require_conn()
        with self._mu:
            row = self.require_task(task_id)
            problems = validate_task_result(result)
            if problems:
                raise AgentError(
                    TASK_RESULT_INVALID,
                    "invalid task result: " + "; ".join(problems),
                    source="ledger",
                )
            self._emit(task_id_root(row), "task.result_recorded", {"task_id": task_id, "result": result})
            conn.execute(
                "UPDATE tasks SET result = ?, updated_at = ? WHERE task_id = ?",
                (_json(result), _now(), task_id),
            )
            conn.commit()
            return self.get_task(task_id)  # type: ignore[return-value]

    def attach_commit(self, task_id: str, commit: str) -> None:
        """Bind the integration commit to the task's recorded result. Evented
        (task.commit_attached) so a rebuilt projection keeps the binding."""
        with self._mu:
            row = self.require_task(task_id)
            result = row.get("result")
            if not isinstance(result, dict):
                return
            self._emit(row["root_task_id"], "task.commit_attached", {"task_id": task_id, "commit": commit})
            result["commit"] = commit
            self._require_conn().execute(
                "UPDATE tasks SET result = ?, updated_at = ? WHERE task_id = ?",
                (_json(result), _now(), task_id),
            )
            self._require_conn().commit()

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        for key in ("dependencies", "capabilities", "acceptance_criteria"):
            out[key] = json.loads(out.get(key) or "[]")
        for key in ("workspace_scope", "budget"):
            out[key] = json.loads(out.get(key) or "{}")
        out["result"] = json.loads(out["result"]) if out.get("result") else None
        out["requires_review"] = bool(out.get("requires_review", 0))
        return out

    # ------------------------------------------------------------------ workers

    def register_worker(self, record: WorkerRecord) -> dict[str, Any]:
        conn = self._require_conn()
        with self._mu:
            task_row = self.require_task(record.task_id)
            if self.get_worker(record.worker_id) is not None:
                raise AgentError(
                    TASK_ALREADY_EXISTS, f"worker already exists: {record.worker_id}", source="ledger"
                )
            data = record.to_dict()
            self._emit(record.root_task_id or task_id_root(task_row), "worker.registered", {"worker": data})
            conn.execute(
                "INSERT INTO workers (worker_id, task_id, root_task_id, role_id, attempt, status, session_id, "
                "workspace_path, branch, base_commit, budget_used, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.worker_id,
                    record.task_id,
                    record.root_task_id,
                    record.role_id,
                    int(record.attempt),
                    record.status,
                    record.session_id,
                    record.workspace_path,
                    record.branch,
                    record.base_commit,
                    _json(record.budget_used),
                    record.created_at,
                    record.created_at,
                ),
            )
            conn.commit()
            return self.get_worker(record.worker_id)  # type: ignore[return-value]

    def get_worker(self, worker_id: str) -> dict[str, Any] | None:
        with self._mu:
            row = self._require_conn().execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            return self._worker_row(row) if row is not None else None

    def list_workers(self, task_id: str | None = None) -> list[dict[str, Any]]:
        with self._mu:
            conn = self._require_conn()
            if task_id:
                rows = conn.execute("SELECT * FROM workers WHERE task_id = ? ORDER BY attempt ASC, worker_id", (task_id,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM workers ORDER BY created_at DESC, worker_id").fetchall()
            return [self._worker_row(r) for r in rows]

    def set_worker_session(self, worker_id: str, session_id: str) -> None:
        """Bind the worker attempt to its session. Emitted as an event so a
        rebuilt projection keeps the binding (resume depends on it)."""
        with self._mu:
            row = self.get_worker(worker_id)
            if row is None:
                raise AgentError(TASK_NOT_FOUND, f"unknown worker: {worker_id}", source="ledger")
            if not str(session_id).strip():
                raise AgentError(TASK_MESSAGE_INVALID, "session_id must not be empty", source="ledger")
            self._emit(row["root_task_id"], "worker.session_bound", {"worker_id": worker_id, "task_id": row["task_id"], "session_id": session_id})
            self._require_conn().execute(
                "UPDATE workers SET session_id = ?, updated_at = ? WHERE worker_id = ?",
                (session_id, _now(), worker_id),
            )
            self._require_conn().commit()

    def set_worker_status(self, worker_id: str, new_status: str, reason: str = "") -> dict[str, Any]:
        conn = self._require_conn()
        with self._mu:
            row = conn.execute("SELECT * FROM workers WHERE worker_id = ?", (worker_id,)).fetchone()
            if row is None:
                raise AgentError(TASK_NOT_FOUND, f"unknown worker: {worker_id}", source="ledger")
            current = row["status"]
            if not can_transition(current, new_status):
                raise AgentError(
                    TASK_TRANSITION_INVALID,
                    f"illegal worker transition {current} -> {new_status} for {worker_id}"
                    + ("; terminal attempts are never revived — register a new attempt" if current in ("completed", "failed", "cancelled") else ""),
                    source="ledger",
                )
            self._emit(
                row["root_task_id"],
                "worker.status_changed",
                {"worker_id": worker_id, "task_id": row["task_id"], "from": current, "to": new_status, "reason": reason},
            )
            conn.execute("UPDATE workers SET status = ?, updated_at = ? WHERE worker_id = ?", (new_status, _now(), worker_id))
            conn.commit()
            return self.get_worker(worker_id)  # type: ignore[return-value]

    @staticmethod
    def _worker_row(row: sqlite3.Row) -> dict[str, Any]:
        out = dict(row)
        out["budget_used"] = json.loads(out.get("budget_used") or "{}")
        return out

    # ------------------------------------------------------------------ messages

    def record_message(self, message: TaskMessage) -> bool:
        """Record a protocol message. Returns False for a redelivered
        message_id: the duplicate causes no event and no second row."""
        conn = self._require_conn()
        if not str(message.message_id).strip():
            raise AgentError(TASK_MESSAGE_INVALID, "message_id must not be empty", source="ledger")
        with self._mu:
            exists = conn.execute("SELECT 1 FROM messages WHERE message_id = ?", (message.message_id,)).fetchone()
            if exists:
                return False
            root = message.root_task_id or (task_id_root(self.require_task(message.task_id)) if message.task_id else "")
            if not root:
                raise AgentError(
                    TASK_MESSAGE_INVALID,
                    f"message {message.message_id} carries neither root_task_id nor a resolvable task_id",
                    source="ledger",
                )
            # Event first: a crash before the INSERT leaves a replayable event,
            # never an unaccounted row. A cross-process duplicate INSERT (cannot
            # happen through this API while the root log lock is held, but kept
            # as defense) collapses on rebuild via INSERT OR IGNORE.
            self._emit(root, "task.message", {"message": message.to_dict()})
            try:
                conn.execute(
                    "INSERT INTO messages (message_id, root_task_id, task_id, worker_id, attempt, message_type, "
                    "correlation_id, payload, created_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        message.message_id,
                        root,
                        message.task_id,
                        message.worker_id,
                        int(message.attempt),
                        message.message_type,
                        message.correlation_id,
                        _json(message.payload),
                        message.created_at,
                        _now(),
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                return False
            return True

    def list_messages(self, root_task_id: str | None = None) -> list[dict[str, Any]]:
        with self._mu:
            conn = self._require_conn()
            if root_task_id:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE root_task_id = ? ORDER BY recorded_at ASC, message_id", (root_task_id,)
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM messages ORDER BY recorded_at ASC, message_id").fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.get("payload") or "{}")
                out.append(item)
            return out

    def pending_messages(
        self,
        root_task_id: str,
        message_type: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Unprocessed protocol messages, oldest first (the pump's queue)."""
        with self._mu:
            conn = self._require_conn()
            sql = "SELECT * FROM messages WHERE root_task_id = ? AND processed_at = ''"
            params: list[Any] = [root_task_id]
            if message_type is not None:
                sql += " AND message_type = ?"
                params.append(message_type)
            sql += " ORDER BY recorded_at ASC, message_id"
            if limit is not None:
                sql += " LIMIT ?"
                params.append(int(limit))
            rows = conn.execute(sql, params).fetchall()
            out = []
            for row in rows:
                item = dict(row)
                item["payload"] = json.loads(item.get("payload") or "{}")
                out.append(item)
            return out

    def mark_message_processed(self, message_id: str) -> None:
        with self._mu:
            self._require_conn().execute(
                "UPDATE messages SET processed_at = ? WHERE message_id = ?",
                (_now(), message_id),
            )
            self._require_conn().commit()

    # ------------------------------------------------------------------ summary / rebuild

    def summary(self) -> dict[str, Any]:
        with self._mu:
            conn = self._require_conn()
            rows = conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
            by_status = {r["status"]: r["n"] for r in rows}
            workers = conn.execute("SELECT COUNT(*) AS n FROM workers").fetchone()["n"]
            messages = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
            return {"total": sum(by_status.values()), "by_status": by_status, "workers": workers, "messages": messages}

    def rebuild(self) -> dict[str, int]:
        """Drop and re-derive the projection from the task event logs.

        Replay applies historical transitions without re-validating them (they
        were legal when written); anything else unexpected in a log fails
        closed as SESSION_CORRUPTED."""
        conn = self._require_conn()
        counts = {"tasks": 0, "workers": 0, "messages": 0, "logs": 0}
        with self._mu:
            conn.executescript("DROP TABLE IF EXISTS messages; DROP TABLE IF EXISTS workers; DROP TABLE IF EXISTS tasks;")
            conn.executescript(_SCHEMA_V2)
            conn.commit()
            tasks_dir = Path(self.config.tasks_dir)
            log_dirs = sorted(p for p in tasks_dir.iterdir() if (p / "events.jsonl").exists()) if tasks_dir.exists() else []
            for log_dir in log_dirs:
                counts["logs"] += 1
                for evt in read_events(log_dir):
                    self._replay(evt, counts)
            conn.commit()
        return counts

    def _replay(self, evt: dict[str, Any], counts: dict[str, int]) -> None:
        conn = self._require_conn()
        etype = evt.get("type", "")
        payload = evt.get("payload") or {}
        ts = evt.get("ts", "")
        if etype == "task.created":
            spec = TaskSpec.from_dict(payload.get("task") or {})
            conn.execute(
                f"INSERT INTO tasks ({_TASK_COLS}) VALUES ({','.join('?' * 19)})",
                (
                    spec.task_id,
                    spec.root_task_id or spec.task_id,
                    spec.parent_task_id,
                    spec.title,
                    spec.description,
                    spec.task_kind,
                    "created",
                    spec.role,
                    _json(spec.dependencies),
                    _json(spec.capabilities),
                    _json(spec.workspace_scope.to_dict()),
                    _json(spec.acceptance_criteria),
                    _json(spec.budget.to_dict()),
                    spec.verification,
                    1 if spec.requires_review else 0,
                    None,
                    "",
                    spec.created_at or ts,
                    spec.created_at or ts,
                ),
            )
            counts["tasks"] += 1
        elif etype == "task.status_changed":
            cur = conn.execute("SELECT status FROM tasks WHERE task_id = ?", (payload.get("task_id", ""),)).fetchone()
            if cur is None:
                raise AgentError(
                    "SESSION_CORRUPTED",
                    f"task log replay: status change for unknown task {payload.get('task_id')!r}",
                    source="ledger",
                )
            conn.execute(
                "UPDATE tasks SET status = ?, error = ?, updated_at = ? WHERE task_id = ?",
                (payload.get("to", cur["status"]), payload.get("reason", ""), ts, payload.get("task_id", "")),
            )
        elif etype == "worker.registered":
            rec = WorkerRecord.from_dict(payload.get("worker") or {})
            conn.execute(
                "INSERT OR REPLACE INTO workers (worker_id, task_id, root_task_id, role_id, attempt, status, "
                "session_id, workspace_path, branch, base_commit, budget_used, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rec.worker_id,
                    rec.task_id,
                    rec.root_task_id,
                    rec.role_id,
                    int(rec.attempt),
                    rec.status,
                    rec.session_id,
                    rec.workspace_path,
                    rec.branch,
                    rec.base_commit,
                    _json(rec.budget_used),
                    rec.created_at or ts,
                    rec.created_at or ts,
                ),
            )
            counts["workers"] += 1
        elif etype == "worker.status_changed":
            cur = conn.execute(
                "UPDATE workers SET status = ?, updated_at = ? WHERE worker_id = ?",
                (payload.get("to", ""), ts, payload.get("worker_id", "")),
            )
            if cur.rowcount == 0:
                raise AgentError(
                    SESSION_CORRUPTED,
                    f"task log replay: worker status change for unknown worker {payload.get('worker_id')!r}",
                    source="ledger",
                )
        elif etype == "worker.session_bound":
            conn.execute(
                "UPDATE workers SET session_id = ?, updated_at = ? WHERE worker_id = ?",
                (payload.get("session_id", ""), ts, payload.get("worker_id", "")),
            )
        elif etype == "task.message":
            msg = TaskMessage.from_dict(payload.get("message") or {})
            cur = conn.execute(
                "INSERT OR IGNORE INTO messages (message_id, root_task_id, task_id, worker_id, attempt, "
                "message_type, correlation_id, payload, created_at, recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    msg.message_id,
                    msg.root_task_id,
                    msg.task_id,
                    msg.worker_id,
                    int(msg.attempt),
                    msg.message_type,
                    msg.correlation_id,
                    _json(msg.payload),
                    msg.created_at or ts,
                    ts,
                ),
            )
            counts["messages"] += cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        elif etype == "task.result_recorded":
            conn.execute(
                "UPDATE tasks SET result = ?, updated_at = ? WHERE task_id = ?",
                (_json(payload.get("result") or {}), ts, payload.get("task_id", "")),
            )
        elif etype == "task.commit_attached":
            row = conn.execute("SELECT result FROM tasks WHERE task_id = ?", (payload.get("task_id", ""),)).fetchone()
            if row is not None and row["result"]:
                result = json.loads(row["result"])
                result["commit"] = payload.get("commit", "")
                conn.execute(
                    "UPDATE tasks SET result = ?, updated_at = ? WHERE task_id = ?",
                    (_json(result), ts, payload.get("task_id", "")),
                )
        # unknown types are ignored (forward compatibility, v2 §7.5)


# ------------------------------------------------------------------ helpers


def task_id_root(task_row: dict[str, Any]) -> str:
    return str(task_row.get("root_task_id") or task_row.get("task_id") or "")


def task_spec_from_row(row: dict[str, Any]) -> TaskSpec:
    """Rebuild the TaskSpec contract from a ledger row (projection columns
    status/result/error/updated_at are not part of the contract)."""
    data = {k: v for k, v in row.items() if k not in ("status", "result", "error", "updated_at")}
    return TaskSpec.from_dict(data)


def _now() -> str:
    from .domain import utcnow_iso

    return utcnow_iso()
