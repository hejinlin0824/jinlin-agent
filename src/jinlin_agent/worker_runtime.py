"""Worker Runtime (v2.0 WP-V2.0-2; ADR-001, ADR-002, ADR-009).

Workers are the ONE existing Harness kernel instantiated per attempt with a
RoleSpec (binding role instruction + tool whitelist), a TaskSpec-derived
budget and a cooperative cancellation check. This module adds lifecycle,
concurrency and retry semantics around the kernel; it deliberately contains
no second execution loop.

Lifecycle (worker rows in the Task Ledger are the authority; the task event
stream records every transition):

    submit_task -> worker.queued  (task created -> queued)
    thread start -> worker.running (task -> running)
    harness.run() outcome:
        completed            -> worker.completed, task.completed + task.result_recorded
        failed               -> worker.failed (+ task.result_recorded, status failed);
                                run_task retries with attempt+1 until
                                TaskSpec.budget.max_attempts, EXCEPT when the
                                error is non-retryable (BUDGET_EXCEEDED) —
                                ADR-009: budget exhaustion never burns a retry
        cancelled            -> worker.cancelled, task.cancelled
        waiting_user/        -> worker.waiting_*, task.waiting_*;
        waiting_approval        resume_worker continues the same session

Workspace model (WP-2 scope; WP-V2.0-5 replaces it with worktrees, ADR-005):
a worker runs against the workspace path it is given, and the SAME workspace
is exclusive for the whole run — the Windows sandbox grants/revokes a
workspace write ACE per instance, so overlapping instances on one workspace
would race the ACL. Different workspaces run in parallel, bounded by
`runtime.max_concurrent_workers` (project layer may only decrease, ADR-009).

Cancellation is cooperative: `cancel_check` fires at step boundaries, so the
in-flight model call or tool finishes first. That is honest step-boundary
cancellation, not a hard kill; per-command process-tree kills exist in the
sandbox (Job Object) and process-level worker isolation is a later decision
(v2 §13.4).
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from .errors import (
    TASK_ALREADY_EXISTS,
    TASK_NOT_FOUND,
    TASK_TRANSITION_INVALID,
    AgentError,
)
from .ledger import TaskLedger, task_spec_from_row
from .loop import Harness
from .model import create_provider
from .sandbox import create_sandbox
from .session import SessionManager
from .taskmodel import (
    SESSION_TO_TASK_STATUS,
    DEFAULT_ROLE_SPECS,
    WorkerRecord,
    can_transition,
    new_worker_id,
)

# ADR-009: budget exhaustion means "stop", not "try again in a fresh attempt".
NON_RETRYABLE_ERROR_CODES = frozenset({"BUDGET_EXCEEDED"})

_TERMINAL_WORKER_STATUSES = {"completed", "failed", "cancelled"}


class WorkerRuntime:
    def __init__(
        self,
        config: Any,
        ledger: TaskLedger,
        provider_factory: Callable[[], Any] | None = None,
        sandbox_factory: Callable[[Path], Any] | None = None,
        log: Callable[[str], None] = lambda msg: None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.provider_factory = provider_factory or (lambda: create_provider(config))
        self.sandbox_factory = sandbox_factory or (lambda ws: create_sandbox(ws, config))
        self.log = log or (lambda msg: None)
        max_workers = max(1, int(config.runtime.get("max_concurrent_workers", 2)))
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="jinlin-worker")
        self._mu = threading.RLock()
        self._futures: dict[str, Future] = {}
        self._cancel_flags: dict[str, threading.Event] = {}
        self._workspace_locks: dict[str, threading.Lock] = {}
        self._outcomes: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ public api

    def submit_task(
        self,
        task_id: str,
        workspace: Path | str,
        role_spec: Any | None = None,
        branch: str = "",
        base_commit: str = "",
    ) -> str:
        """Register a new worker attempt for the task and queue it. One active
        worker per task; a retry is another submit (run_task drives that).
        branch/base_commit record worktree isolation when a WorkspaceManager
        assigned one (WP-V2.0-5)."""
        with self._mu:
            row = self.ledger.require_task(task_id)
            active = [w for w in self.ledger.list_workers(task_id) if w["status"] not in _TERMINAL_WORKER_STATUSES]
            if active:
                raise AgentError(
                    TASK_ALREADY_EXISTS,
                    f"task {task_id} already has an active worker: {active[0]['worker_id']} ({active[0]['status']})",
                    source="runtime",
                )
            spec = task_spec_from_row(row)
            attempt = len(self.ledger.list_workers(task_id)) + 1
            worker_id = new_worker_id()
            record = WorkerRecord(
                worker_id=worker_id,
                task_id=task_id,
                root_task_id=row["root_task_id"],
                role_id=spec.role,
                attempt=attempt,
                status="queued",
                workspace_path=str(workspace),
                branch=branch,
                base_commit=base_commit,
            )
            self.ledger.register_worker(record)
            if row["status"] == "created":
                self.ledger.set_task_status(task_id, "queued", reason=f"worker {worker_id} queued")
            self._cancel_flags[worker_id] = threading.Event()
            self._futures[worker_id] = self._executor.submit(
                self._run_attempt, spec, record, role_spec, self._cancel_flags[worker_id]
            )
            return worker_id

    def run_task(self, task_id: str, workspace: Path | str, role_spec: Any | None = None) -> dict[str, Any]:
        """Run the task to a final outcome, retrying failed attempts with a new
        worker up to TaskSpec.budget.max_attempts (ADR-009)."""
        spec = task_spec_from_row(self.ledger.require_task(task_id))
        max_attempts = max(1, int(spec.budget.max_attempts))
        outcome: dict[str, Any] = {}
        for _ in range(max_attempts):
            worker_id = self.submit_task(task_id, workspace, role_spec)
            outcome = self.wait_worker(worker_id)
            if outcome["status"] != "failed":
                return outcome
            if outcome.get("error_code") in NON_RETRYABLE_ERROR_CODES:
                self.log(f"[runtime] task {task_id}: {outcome.get('error_code')} — not retryable")
                break
            self.log(
                f"[runtime] task {task_id}: attempt {outcome.get('attempt')} failed "
                f"({outcome.get('error_code')}); retrying with a new attempt"
            )
        if outcome.get("status") == "failed":
            self._advance_task(task_id, "failed", reason=f"all attempts exhausted ({outcome.get('error_code', 'failed')})")
        return outcome

    def wait_worker(self, worker_id: str, timeout: float | None = None) -> dict[str, Any]:
        future = self._futures.get(worker_id)
        if future is None:
            raise AgentError(TASK_NOT_FOUND, f"unknown or already-collected worker: {worker_id}", source="runtime")
        future.result(timeout=timeout)  # a crashed attempt must not be swallowed
        outcome = self._outcomes.get(worker_id)
        if outcome is None:
            raise AgentError(TASK_NOT_FOUND, f"worker produced no outcome: {worker_id}", source="runtime")
        return dict(outcome)

    def cancel_worker(self, worker_id: str, reason: str = "") -> None:
        row = self.ledger.get_worker(worker_id)
        if row is None:
            raise AgentError(TASK_NOT_FOUND, f"unknown worker: {worker_id}", source="runtime")
        flag = self._cancel_flags.get(worker_id)
        if flag is not None:
            flag.set()  # cooperative stop at the next step boundary
        with self._mu:
            current = self.ledger.get_worker(worker_id)
            if current is not None and current["status"] == "queued":
                # never started: cancel it directly, no execution to stop
                self.ledger.set_worker_status(worker_id, "cancelled", reason=reason or "cancelled before start")
                self._advance_task(current["task_id"], "cancelled", reason=reason or "cancelled before start")

    def resume_worker(self, worker_id: str, answer: str = "", role_spec: Any | None = None) -> dict[str, Any]:
        """Continue a waiting worker's session (v2 §13.3: ask user -> resolve)."""
        with self._mu:
            row = self.ledger.get_worker(worker_id)
            if row is None:
                raise AgentError(TASK_NOT_FOUND, f"unknown worker: {worker_id}", source="runtime")
            if row["status"] not in ("waiting_user", "waiting_approval"):
                raise AgentError(
                    TASK_TRANSITION_INVALID,
                    f"worker {worker_id} is {row['status']}; only waiting workers can be resumed",
                    source="runtime",
                )
            spec = task_spec_from_row(self.ledger.require_task(row["task_id"]))
            role = role_spec if role_spec is not None else DEFAULT_ROLE_SPECS.get(spec.role)
            record = WorkerRecord.from_dict(row)
            self._cancel_flags[worker_id] = threading.Event()
            self._futures[worker_id] = self._executor.submit(
                self._run_resume, spec, record, role, self._cancel_flags[worker_id], answer
            )
        return self.wait_worker(worker_id)

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)

    # ------------------------------------------------------------------ attempt execution

    def _run_attempt(self, spec: Any, record: WorkerRecord, role_spec: Any, cancel_flag: threading.Event) -> None:
        workspace = Path(record.workspace_path)
        role = role_spec if role_spec is not None else DEFAULT_ROLE_SPECS.get(spec.role)
        session = None
        try:
            with self._workspace_lock(str(workspace)):
                with self._mu:
                    current = self.ledger.get_worker(record.worker_id)
                    if current is not None and (cancel_flag.is_set() or current["status"] == "cancelled"):
                        if current["status"] == "queued":
                            self.ledger.set_worker_status(record.worker_id, "cancelled", reason="cancelled before start")
                            self._advance_task(spec.task_id, "cancelled", reason="cancelled before start")
                        self._store_outcome(record, status="cancelled", session_id="", summary="cancelled before start", error_code="USER_CANCELLED")
                        return
                self.ledger.set_worker_status(record.worker_id, "running", reason=f"attempt {record.attempt} started")
                self._advance_task(spec.task_id, "running", reason=f"attempt {record.attempt} started")
                sandbox = self.sandbox_factory(workspace)
                session = SessionManager.create(self.config, workspace, self._request_text(spec), sandbox)
                self.ledger.set_worker_session(record.worker_id, session.id)
                harness = self._build_harness(session, role, spec, cancel_flag)
                status = harness.run()
                self._finish_attempt(spec, record, session, status)
        except Exception as exc:  # the runtime itself failed: say so, never hang silently
            self.log(f"[runtime] worker {record.worker_id} crashed: {type(exc).__name__}: {exc}")
            try:
                self.ledger.set_worker_status(record.worker_id, "failed", reason=f"runtime crash: {type(exc).__name__}: {exc}")
            except Exception:
                pass
            self._store_outcome(
                record,
                status="failed",
                session_id=session.id if session is not None else "",
                summary=f"runtime crash: {type(exc).__name__}: {exc}",
                error_code="RUNTIME_CRASH",
            )
        finally:
            if session is not None:
                session.close()

    def _run_resume(self, spec: Any, row: WorkerRecord, role: Any, cancel_flag: threading.Event, answer: str) -> None:
        workspace = Path(row.workspace_path)
        session = None
        try:
            with self._workspace_lock(str(workspace)):
                self.ledger.set_worker_status(row.worker_id, "running", reason="resumed")
                self._advance_task(row.task_id, "running", reason="worker resumed")
                sandbox = self.sandbox_factory(workspace)
                session = SessionManager.open(self.config, row.session_id, sandbox)
                if answer.strip():
                    session.emit("session.user_input", {"text": answer.strip()})
                harness = self._build_harness(session, role, spec, cancel_flag)
                status = harness.run()
                self._finish_attempt(spec, row, session, status)
        except Exception as exc:
            self.log(f"[runtime] worker {row.worker_id} crashed on resume: {type(exc).__name__}: {exc}")
            try:
                self.ledger.set_worker_status(row.worker_id, "failed", reason=f"runtime crash: {type(exc).__name__}: {exc}")
            except Exception:
                pass
            self._store_outcome(
                row,
                status="failed",
                session_id=session.id if session is not None else row.session_id,
                summary=f"runtime crash: {type(exc).__name__}: {exc}",
                error_code="RUNTIME_CRASH",
            )
        finally:
            if session is not None:
                session.close()

    # ------------------------------------------------------------------ shared pieces

    def _build_harness(self, session: Any, role: Any, spec: Any, cancel_flag: threading.Event) -> Harness:
        budget = {
            "max_tokens": int(spec.budget.max_tokens),
            "max_tool_calls": int(spec.budget.max_tool_calls),
            "timeout_seconds": int(spec.budget.timeout_seconds),
        }
        # task capability set + workspace scope become enforced policy (ADR-006)
        caps = {str(c).strip().upper() for c in (spec.capabilities or []) if str(c).strip()}
        scope = spec.workspace_scope if (spec.workspace_scope and (spec.workspace_scope.read or spec.workspace_scope.write)) else None
        policy = None
        if caps or scope is not None:
            from .policy import PolicyEngine

            policy = PolicyEngine(
                self.config,
                session.workspace,
                approval_handler=None,
                auto_approve=False,
                read_only=False,
                task_capabilities=caps or None,
                workspace_scope=scope,
            )
        return Harness(
            session=session,
            config=self.config,
            provider=self.provider_factory(),
            role=role,
            budget=budget,
            cancel_check=cancel_flag.is_set,
            policy=policy,
            log=self.log,
        )

    def _finish_attempt(self, spec: Any, record: WorkerRecord, session: Any, status: str) -> None:
        state = session.state
        task_status = SESSION_TO_TASK_STATUS.get(status, "failed")
        reason = {
            "completed": f"attempt {record.attempt} completed",
            "failed": f"attempt {record.attempt} failed",
            "cancelled": f"attempt {record.attempt} cancelled",
        }.get(status, f"attempt {record.attempt} -> {status}")
        self.ledger.set_worker_status(record.worker_id, task_status, reason=reason)

        error_code = (state.last_error or {}).get("code", "") if status == "failed" else ""
        if status == "completed":
            completion = state.completion or {}
            result = self._build_result(state, "completed", str(completion.get("summary", "")))
            self.ledger.record_result(record.task_id, result)
            task_row = self.ledger.get_task(record.task_id) or {}
            if task_row.get("requires_review"):
                # the deliverable is NOT accepted yet: an independent review
                # completes it (or fails it) — never a terminal state first
                self._advance_task(record.task_id, "review_pending", reason="awaiting independent review")
            else:
                self._advance_task(record.task_id, "completed", reason=reason)
        elif status == "failed":
            message = str((state.last_error or {}).get("message", "attempt failed"))
            result = self._build_result(state, "failed", message)
            self.ledger.record_result(record.task_id, result)
            # task-level `failed` is set by run_task only when attempts are
            # exhausted; a retryable failure leaves the task running
        else:
            self._advance_task(record.task_id, task_status, reason=reason)
        self._store_outcome(
            record,
            status=task_status,
            session_id=session.id,
            summary=str((state.completion or {}).get("summary", "")) if status == "completed" else str((state.last_error or {}).get("message", reason)),
            error_code=error_code,
        )

    def _build_result(self, state: Any, status: str, summary: str) -> dict[str, Any]:
        tests = []
        for run in state.verification_runs[-4:]:
            command = " ".join(run.command) if run.command else f"{run.level} {run.type}"
            tests.append({"command": command, "status": run.status, "exit_code": run.exit_code})
        return {
            "status": status,
            "summary": summary or ("completed" if status == "completed" else "attempt failed"),
            "changed_files": state.diff_paths(),
            "commit": "",  # commit binding arrives with WP-V2.0-5 (worktrees)
            "tests": tests,
            "known_risks": [],
            "follow_up": [],
        }

    def _request_text(self, spec: Any) -> str:
        lines = [spec.title or spec.task_id]
        if spec.description.strip():
            lines.append("")
            lines.append(spec.description.strip())
        if spec.acceptance_criteria:
            lines.append("")
            lines.append("Acceptance criteria:")
            lines.extend(f"- {c}" for c in spec.acceptance_criteria)
        return "\n".join(lines)

    def _advance_task(self, task_id: str, new_status: str, reason: str = "") -> None:
        row = self.ledger.get_task(task_id)
        if row is None:
            return
        if can_transition(row["status"], new_status):
            self.ledger.set_task_status(task_id, new_status, reason=reason)

    def _workspace_lock(self, workspace: str) -> threading.Lock:
        with self._mu:
            key = os.path.normcase(os.path.abspath(workspace))
            return self._workspace_locks.setdefault(key, threading.Lock())

    def _store_outcome(
        self,
        record: WorkerRecord,
        status: str,
        session_id: str,
        summary: str,
        error_code: str = "",
    ) -> None:
        task_row = self.ledger.get_task(record.task_id) or {}
        with self._mu:
            self._outcomes[record.worker_id] = {
                "worker_id": record.worker_id,
                "attempt": record.attempt,
                "task_id": record.task_id,
                "status": status,
                "task_status": task_row.get("status", ""),
                "session_id": session_id,
                "summary": summary,
                "error_code": error_code,
                "result": task_row.get("result"),
            }
