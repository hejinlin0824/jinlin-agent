"""In-process lifecycle manager for local Workbench tasks.

Tasks execute through ApplicationService; this module owns only background
thread coordination and browser approval rendezvous. It is intentionally
single-task-at-a-time and keeps no second agent loop or durable task database.
"""
from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass, field
from typing import Any

from .errors import AgentError

_ACTIVE = {"queued", "running", "waiting_approval"}


@dataclass
class WorkbenchTask:
    task_id: str
    request: str
    read_only: bool
    status: str = "queued"
    session_id: str = ""
    error: dict[str, str] | None = None
    pending_approval: dict[str, Any] | None = None
    created_thread: threading.Thread | None = field(default=None, repr=False)


class WorkbenchTaskManager:
    """Coordinates one background task and fail-closed approval decisions."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self._condition = threading.Condition(threading.RLock())
        self._tasks: dict[str, WorkbenchTask] = {}

    def submit(self, request: str, *, read_only: bool = True) -> dict[str, Any]:
        if not isinstance(request, str) or not request.strip():
            raise AgentError("REQUEST_INVALID", "task must be a non-empty string", source="workbench")
        if len(request) > 20_000:
            raise AgentError("REQUEST_INVALID", "task exceeds 20000 characters", source="workbench")
        if not isinstance(read_only, bool):
            raise AgentError("REQUEST_INVALID", "read_only must be a boolean", source="workbench")
        task = WorkbenchTask(
            task_id="task_" + secrets.token_urlsafe(18),
            request=request.strip(),
            read_only=read_only,
        )
        with self._condition:
            if any(item.status in _ACTIVE for item in self._tasks.values()):
                raise AgentError("TASK_BUSY", "another Workbench task is active; wait for it to finish", source="workbench")
            self._tasks[task.task_id] = task
            thread = threading.Thread(
                target=self._run,
                args=(task,),
                name="jinlin-workbench-" + task.task_id[-8:],
                daemon=True,
            )
            task.created_thread = thread
            thread.start()
        return self.get(task.task_id)

    def _run(self, task: WorkbenchTask) -> None:
        with self._condition:
            task.status = "running"
            self._condition.notify_all()

        def on_session(session: Any, _sandbox: Any) -> None:
            with self._condition:
                task.session_id = session.id
                self._condition.notify_all()

        def approve(request: Any) -> tuple[str, str]:
            safe_request = {
                "approval_id": str(getattr(request, "approval_id", "")),
                "tool": str(getattr(request, "tool", ""))[:80],
                "risk": str(getattr(request, "risk", "unknown"))[:32],
                "summary": str(getattr(request, "summary", ""))[:1000],
                "reason": str(getattr(request, "reason", ""))[:1000],
                "impact": [str(x)[:300] for x in (getattr(request, "impact", []) or [])[:10]],
            }
            with self._condition:
                task.pending_approval = safe_request
                task.status = "waiting_approval"
                self._condition.notify_all()
                while task.pending_approval is not None:
                    self._condition.wait()
                decision = getattr(task, "_approval_decision", None)
                note = getattr(task, "_approval_note", "")
                task._approval_decision = None
                task._approval_note = ""
                return (decision or "denied", note or "approval was not provided")

        try:
            _session, status = self.service.run(
                task.request,
                read_only=task.read_only,
                auto_approve=False,
                approval_handler=approve,
                on_session=on_session,
            )
            with self._condition:
                task.status = "waiting_user" if status == "waiting_user" else status
        except AgentError as exc:
            with self._condition:
                task.status = "failed"
                task.error = {"code": exc.code, "message": exc.message[:1000]}
        except Exception:
            with self._condition:
                task.status = "failed"
                task.error = {"code": "INTERNAL_ERROR", "message": "task execution failed; inspect the session event log"}
        finally:
            with self._condition:
                task.pending_approval = None
                self._condition.notify_all()

    def approve(self, task_id: str, approval_id: str, decision: str) -> dict[str, Any]:
        if decision not in ("approved_once", "approved_for_session", "denied"):
            raise AgentError("REQUEST_INVALID", "decision must be approved_once, approved_for_session, or denied", source="workbench")
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None:
                raise AgentError("TASK_NOT_FOUND", "task not found", source="workbench")
            pending = task.pending_approval
            if task.status != "waiting_approval" or not pending or not secrets.compare_digest(
                str(pending.get("approval_id", "")), str(approval_id)
            ):
                raise AgentError("APPROVAL_STALE", "approval is no longer pending or does not match", source="workbench")
            task._approval_decision = decision
            task._approval_note = "approved in local Workbench" if decision != "denied" else "denied in local Workbench"
            task.pending_approval = None
            task.status = "running"
            self._condition.notify_all()
        return self.get(task_id)

    def get(self, task_id: str) -> dict[str, Any]:
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None:
                raise AgentError("TASK_NOT_FOUND", "task not found", source="workbench")
            return self._snapshot(task)

    def list(self) -> list[dict[str, Any]]:
        with self._condition:
            return [self._snapshot(task) for task in list(reversed(list(self._tasks.values())))[:50]]

    @staticmethod
    def _snapshot(task: WorkbenchTask) -> dict[str, Any]:
        return {
            "task_id": task.task_id,
            "request": task.request[:240],
            "read_only": task.read_only,
            "status": task.status,
            "session_id": task.session_id or None,
            "error": task.error,
            "pending_approval": dict(task.pending_approval) if task.pending_approval else None,
        }
