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
    APPROVAL_REQUIRED,
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
    TaskMessage,
    WorkerRecord,
    can_transition,
    new_message_id,
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

    def resume_worker(
        self,
        worker_id: str,
        answer: str = "",
        role_spec: Any | None = None,
        approval_decision: str | None = None,
    ) -> dict[str, Any]:
        """Continue a waiting worker's session (v2 §13.3).

        WP-R7: `waiting_user` resumes with free TEXT (information); 
        `waiting_approval` is a PERMISSION decision and cannot be granted by
        text — it requires an explicit approval_decision:

        * ``denied``        -> the session resumes with the operation denied;
        * ``approved_once`` -> NOT supported through the headless worker path:
          the kernel already fail-closed that approval, and a worker-runtime
          re-grant would be a policy bypass. Interactive CLI approval is the
          only grant path (recorded honestly; an HTTP approval queue with a
          persisted approval registry is future work)."""
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
            if row["status"] == "waiting_approval":
                if approval_decision not in ("approved_once", "denied"):
                    raise AgentError(
                        APPROVAL_REQUIRED,
                        "resuming a worker waiting on APPROVAL requires an explicit "
                        "approval_decision ('approved_once' or 'denied'); free text cannot "
                        "grant permissions",
                        source="runtime",
                    )
                if approval_decision == "approved_once":
                    raise AgentError(
                        APPROVAL_REQUIRED,
                        "worker-runtime approval grants are not supported: the approval was "
                        "fail-closed by the kernel and can only be granted through the "
                        "interactive CLI approval path",
                        source="runtime",
                    )
            spec = task_spec_from_row(self.ledger.require_task(row["task_id"]))
            role = role_spec if role_spec is not None else DEFAULT_ROLE_SPECS.get(spec.role)
            record = WorkerRecord.from_dict(row)
            self._cancel_flags[worker_id] = threading.Event()
            self._futures[worker_id] = self._executor.submit(
                self._run_resume, spec, record, role, self._cancel_flags[worker_id], answer, approval_decision
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

    def _run_resume(
        self,
        spec: Any,
        row: WorkerRecord,
        role: Any,
        cancel_flag: threading.Event,
        answer: str,
        approval_decision: str | None = None,
    ) -> None:
        workspace = Path(row.workspace_path)
        session = None
        try:
            with self._workspace_lock(str(workspace)):
                self.ledger.set_worker_status(row.worker_id, "running", reason="resumed")
                self._advance_task(row.task_id, "running", reason="worker resumed")
                sandbox = self.sandbox_factory(workspace)
                session = SessionManager.open(self.config, row.session_id, sandbox)
                if approval_decision == "denied":
                    # the explicit permission decision: the operation stays denied
                    session.emit(
                        "approval.resolved",
                        {"decision": "denied", "note": "denied from the workbench/task resume path"},
                    )
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
        # WP-R4: effective capabilities = task ∩ role (the system layer is
        # the config gate set enforced inside PolicyEngine). None layers do
        # not constrain; empty layers are hard denies; the intersection can
        # only shrink. The role's allowed_tools whitelist still applies via
        # Harness(role=...) on top.
        from .policy import intersect_capability_layers

        task_layer = (
            {str(c).strip().upper() for c in (spec.capabilities or []) if str(c).strip()}
            if spec.capabilities is not None
            else None
        )
        role_layer = None
        if role is not None and getattr(role, "allowed_capabilities", None) is not None:
            role_layer = {str(c).strip().upper() for c in role.allowed_capabilities if str(c).strip()}
        effective_caps = intersect_capability_layers(task_layer, role_layer)
        scope = spec.workspace_scope if (spec.workspace_scope and (spec.workspace_scope.read or spec.workspace_scope.write)) else None
        policy = None
        if effective_caps is not None or scope is not None:
            from .policy import PolicyEngine

            policy = PolicyEngine(
                self.config,
                session.workspace,
                approval_handler=None,
                auto_approve=False,
                read_only=False,
                task_capabilities=effective_caps,
                workspace_scope=scope,
            )
        context_providers = None
        if spec.dependencies:
            from .context_provider import DependencyContextProvider

            context_providers = [DependencyContextProvider(self.ledger, list(spec.dependencies))]
        return Harness(
            session=session,
            config=self.config,
            provider=self.provider_factory(),
            role=role,
            budget=budget,
            cancel_check=cancel_flag.is_set,
            policy=policy,
            context_providers=context_providers,
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
        outcome_status = task_status
        outcome_summary = (
            str((state.completion or {}).get("summary", ""))
            if status == "completed"
            else str((state.last_error or {}).get("message", reason))
        )
        if status == "completed":
            completion = state.completion or {}
            result = self._build_result(state, "completed", str(completion.get("summary", "")))
            self.ledger.record_result(record.task_id, result)
            task_row = self.ledger.get_task(record.task_id) or {}
            # acceptance gate (WP-V2.0-8): the session succeeded, but the task
            # is only COMPLETED when its verification contract is satisfied.
            # Runs here in the worker thread because only this context knows
            # whether the project even HAS a test command (v2 §10.2).
            from .verify import check_task_verification, detect_project

            project = detect_project(session.workspace, self.config)
            reasons = check_task_verification(spec, result, has_test_command=bool(project.test_commands))
            if reasons:
                self._advance_task(
                    record.task_id,
                    "failed",
                    reason="task verification gate: " + "; ".join(reasons),
                )
                outcome_status = "failed"
                outcome_summary = "task verification gate: " + "; ".join(reasons)
                error_code = "VERIFICATION_GATE"
            elif task_row.get("requires_review"):
                # the deliverable is NOT accepted yet: an independent review
                # completes it (or fails it) — never a terminal state first
                self._advance_task(record.task_id, "review_pending", reason="awaiting independent review")
                outcome_status = "review_pending"
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
            if status == "waiting_approval" and (pending := getattr(state, "pending_approval", None)):
                try:
                    self.ledger.record_message(
                        TaskMessage(
                            message_id=new_message_id(),
                            root_task_id=row.root_task_id or task_id,
                            task_id=record.task_id,
                            worker_id=record.worker_id,
                            attempt=record.attempt,
                            message_type="task.approval_required",
                            payload={"approval": session_state_pending},
                        )
                    )
                except Exception:
                    pass  # visibility best-effort; the session events hold the truth
        self._store_outcome(
            record,
            status=outcome_status,
            session_id=session.id,
            summary=outcome_summary,
            error_code=error_code,
        )

    def _build_result(self, state: Any, status: str, summary: str) -> dict[str, Any]:
        # keep the LATEST run per level (same semantics as the session gate's
        # by_level map) — a truncating window could drop a required level
        by_level: dict[str, Any] = {}
        for run in state.verification_runs:
            by_level[run.level] = run
        tests = []
        for level in sorted(by_level):
            run = by_level[level]
            command = " ".join(run.command) if run.command else f"{run.level} {run.type}"
            tests.append({"command": command, "level": run.level, "status": run.status, "exit_code": run.exit_code})
        return {
            "status": status,
            "summary": summary or ("completed" if status == "completed" else "attempt failed"),
            "changed_files": state.diff_paths(),
            "commit": "",  # attached by the IntegrationCoordinator (WP-V2.0-5)
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


def recover_interrupted(config: Any, ledger: TaskLedger, log: Any = None) -> dict[str, int]:
    """WP-R5: reconcile worker attempts that were in flight when a process
    died. The SESSION EVENT LOG is the truth for what actually happened:

    * session terminal (completed/failed) -> the worker row is reconciled to
      match and the deliverable/result recorded (integration still enforced
      by the normal gates);
    * session missing or non-terminal (crashed mid-run) -> the worker becomes
      failed(RECOVERY_UNCERTAIN) and its task is BLOCKED for adjudication —
      an unknown state is never promoted to success, and there is never an
      automatic retry (ADR-009);
    * queued/waiting workers are preserved (reschedulable / waiting);
    * cancelled workers are never revived."""
    from pathlib import Path as _Path

    from .errors import RECOVERY_UNCERTAIN, AgentError
    from .events import read_events
    from .state import recover as fold_events
    from .taskmodel import SESSION_TO_TASK_STATUS, can_transition

    log = log or (lambda msg: None)
    report = {"failed_recovered": 0, "completed_reconciled": 0, "preserved": 0}
    for row in ledger.list_workers():
        if row["status"] != "running":
            continue
        worker_id = row["worker_id"]
        task_id = row["task_id"]
        sid = row.get("session_id") or ""
        session_status = ""
        session_state = None
        if sid:
            session_dir = _Path(config.sessions_dir) / sid
            if (session_dir / "events.jsonl").exists():
                try:
                    session_state = fold_events(read_events(session_dir))
                    session_status = session_state.status
                except AgentError as exc:
                    log(f"[recovery] {worker_id}: session {sid} unreadable ({exc.code}); treating as uncertain")
        task_row = ledger.get_task(task_id) or {}

        def _task_advance(new_status: str, reason: str) -> None:
            if task_row and can_transition(task_row["status"], new_status):
                ledger.set_task_status(task_id, new_status, reason=reason)

        if session_status == "completed":
            ledger.set_worker_status(worker_id, "completed", reason="recovered: session completed")
            completion = session_state.completion or {}
            result = {
                "status": "completed",
                "summary": str(completion.get("summary", "")) or "recovered completed session",
                "changed_files": session_state.diff_paths(),
                "commit": "",
                "tests": [],
                "known_risks": ["recovered after restart; verification state carried over from the session log"],
                "follow_up": [],
            }
            try:
                ledger.record_result(task_id, result)
            except Exception:
                pass  # a task whose contract rejects this result stays for adjudication
            _task_advance("completed", "recovered: session completed")
            report["completed_reconciled"] += 1
            continue

        reasons = {
            "failed": "recovered: session recorded a failure",
        }
        if session_status == "failed":
            ledger.set_worker_status(worker_id, "failed", reason=reasons["failed"])
            report["failed_recovered"] += 1
            continue

        # crashed mid-run (or session unreadable): uncertain, fail closed
        ledger.set_worker_status(
            worker_id,
            "failed",
            reason=f"recovery: {RECOVERY_UNCERTAIN} — the session shows no terminal state; "
            "adjudicate the workspace before any retry",
        )
        _task_advance("blocked", reason=f"worker {worker_id} interrupted mid-run; adjudication required")
        report["failed_recovered"] += 1
    return report
