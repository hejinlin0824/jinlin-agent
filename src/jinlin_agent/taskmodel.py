"""Task-layer domain model (v2.0 WP-V2.0-1).

Serializable dataclasses only — no I/O in this module (same discipline as
domain.py). ADR-003: the session event log stays the execution truth; these
types are what the task ledger indexes. ADR-009: terminal statuses are never
revived — a retry is a new attempt, not a state rewind.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .domain import utcnow_iso


def new_task_id() -> str:
    return f"task_{uuid.uuid4().hex[:12]}"


def new_worker_id() -> str:
    return f"worker_{uuid.uuid4().hex[:12]}"


def new_message_id() -> str:
    return f"msg_{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ status machine


class TaskStatus(str, Enum):
    """v2.0 §6.3 lifecycle. The task layer deliberately has no `paused` state:
    waiting_approval / waiting_user / blocked cover it, and the reserved
    session-level `paused` maps onto `blocked` (see SESSION_TO_TASK_STATUS)."""

    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    BLOCKED = "blocked"
    REVIEW_PENDING = "review_pending"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TASK_TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
)

# Legal transitions. Terminal states map to nothing: a completed task is never
# silently rewound to running (ADR-009); retries create a new attempt.
_TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    TaskStatus.CREATED.value: frozenset(
        {TaskStatus.QUEUED.value, TaskStatus.RUNNING.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
    ),
    TaskStatus.QUEUED.value: frozenset({TaskStatus.RUNNING.value, TaskStatus.CANCELLED.value}),
    TaskStatus.RUNNING.value: frozenset(
        {
            TaskStatus.WAITING_APPROVAL.value,
            TaskStatus.WAITING_USER.value,
            TaskStatus.BLOCKED.value,
            TaskStatus.REVIEW_PENDING.value,
            TaskStatus.COMPLETED.value,
            TaskStatus.FAILED.value,
            TaskStatus.CANCELLED.value,
        }
    ),
    # a task whose contract requires_review parks here after the worker
    # finishes: only an accepted review completes it (a rejection fails it) —
    # terminal states are never revived (ADR-009)
    TaskStatus.REVIEW_PENDING.value: frozenset(
        {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
    ),
    TaskStatus.WAITING_APPROVAL.value: frozenset({TaskStatus.RUNNING.value, TaskStatus.CANCELLED.value}),
    TaskStatus.WAITING_USER.value: frozenset({TaskStatus.RUNNING.value, TaskStatus.CANCELLED.value}),
    TaskStatus.BLOCKED.value: frozenset(
        {TaskStatus.RUNNING.value, TaskStatus.FAILED.value, TaskStatus.CANCELLED.value}
    ),
    TaskStatus.COMPLETED.value: frozenset(),
    TaskStatus.FAILED.value: frozenset(),
    TaskStatus.CANCELLED.value: frozenset(),
}


def can_transition(current: str, nxt: str) -> bool:
    """Unknown statuses transition to nothing (fail closed)."""
    return nxt in _TASK_TRANSITIONS.get(str(current), frozenset())


# ADR-003: the task<->session status mapping is frozen here and locked by test.
# The task layer adds `queued` (not yet picked up) and `blocked` (task-level
# blockage) on top of the session statuses; the reserved session `paused`
# (defined in domain.py, never emitted today) holds its task blocked.
SESSION_TO_TASK_STATUS: dict[str, str] = {
    "created": "created",
    "running": "running",
    "waiting_approval": "waiting_approval",
    "waiting_user": "waiting_user",
    "paused": "blocked",
    "failed": "failed",
    "completed": "completed",
    "cancelled": "cancelled",
}


# ------------------------------------------------------------------ contracts


@dataclass
class Budget:
    """Per-task budget (v2 §12.1 + ADR-009). Enforcement arrives with WP-2."""

    max_tokens: int = 300_000
    max_tool_calls: int = 200
    timeout_seconds: int = 3600
    max_attempts: int = 2

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Budget":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d and d[k] is not None})


@dataclass
class WorkspaceScope:
    """Path ranges a task may read/write, relative to the task workspace.
    Data only in WP-1; PolicyEngine enforcement lands with WP-6 (ADR-006)."""

    read: list[str] = field(default_factory=list)
    write: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"read": list(self.read), "write": list(self.write)}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkspaceScope":
        return cls(read=list(d.get("read") or []), write=list(d.get("write") or []))


@dataclass
class TaskSpec:
    """One node of the task graph: a root task or a subtask (v2 §12.1).
    A root task has an empty parent_task_id and root_task_id == task_id."""

    task_id: str
    root_task_id: str = ""
    parent_task_id: str = ""
    title: str = ""
    description: str = ""
    task_kind: str = "implementation"
    dependencies: list[str] = field(default_factory=list)
    role: str = "worker"
    workspace_scope: WorkspaceScope = field(default_factory=WorkspaceScope)
    capabilities: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    verification: str = "code"
    requires_review: bool = False
    created_at: str = field(default_factory=utcnow_iso)

    def is_root(self) -> bool:
        return not self.parent_task_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "root_task_id": self.root_task_id or self.task_id,
            "parent_task_id": self.parent_task_id,
            "title": self.title,
            "description": self.description,
            "task_kind": self.task_kind,
            "dependencies": list(self.dependencies),
            "role": self.role,
            "workspace_scope": self.workspace_scope.to_dict(),
            "capabilities": list(self.capabilities),
            "acceptance_criteria": list(self.acceptance_criteria),
            "budget": self.budget.to_dict(),
            "verification": self.verification,
            "requires_review": self.requires_review,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskSpec":
        scope = d.get("workspace_scope")
        budget = d.get("budget")
        return cls(
            task_id=d["task_id"],
            root_task_id=d.get("root_task_id", ""),
            parent_task_id=d.get("parent_task_id", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            task_kind=d.get("task_kind", "implementation"),
            dependencies=list(d.get("dependencies") or []),
            role=d.get("role", "worker"),
            workspace_scope=WorkspaceScope.from_dict(scope) if isinstance(scope, dict) else WorkspaceScope(),
            capabilities=list(d.get("capabilities") or []),
            acceptance_criteria=list(d.get("acceptance_criteria") or []),
            budget=Budget.from_dict(budget) if isinstance(budget, dict) else Budget(),
            verification=d.get("verification", "code"),
            requires_review=bool(d.get("requires_review", False)),
            created_at=d.get("created_at", ""),
        )


@dataclass
class RoleSpec:
    """Job description for an execution instance (v2 §12.2). Data only in
    WP-1; tool-whitelist enforcement lands with WP-2/WP-6."""

    role_id: str
    description: str = ""
    system_instruction: str = ""
    allowed_capabilities: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    can_delegate: bool = False
    output_schema: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RoleSpec":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


DEFAULT_ROLE_SPECS: dict[str, RoleSpec] = {
    "supervisor": RoleSpec(
        "supervisor",
        description="全局控制、决策、汇总",
        allowed_capabilities=["READ", "WORKFLOW", "USER_INPUT"],
        can_delegate=True,
    ),
    "planner": RoleSpec(
        "planner",
        description="任务分析与 DAG 生成，不直接执行代码修改",
        allowed_capabilities=["READ"],
        allowed_tools=["read_file", "list_dir", "find_files", "search_text", "get_file_info", "get_project_summary"],
        output_schema="TaskPlan",
    ),
    "worker": RoleSpec(
        "worker",
        description="执行具体子任务；能力按 TaskSpec 授权",
        allowed_capabilities=["READ", "WRITE", "EXECUTE"],
    ),
    "reviewer": RoleSpec(
        "reviewer",
        description="独立审查差异与验证证据",
        allowed_capabilities=["READ"],
        output_schema="ReviewResult",
    ),
    "verifier": RoleSpec(
        "verifier",
        description="执行验证阶梯",
        allowed_capabilities=["READ", "EXECUTE"],
    ),
}


# ------------------------------------------------------------------ workers


@dataclass
class WorkerRecord:
    """One execution attempt of one task (v2 §12.3). Retries register a new
    record with attempt+1; terminal records are immutable."""

    worker_id: str
    task_id: str
    root_task_id: str = ""
    role_id: str = "worker"
    attempt: int = 1
    status: str = TaskStatus.CREATED.value
    session_id: str = ""
    workspace_path: str = ""
    branch: str = ""
    base_commit: str = ""
    budget_used: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "WorkerRecord":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# ------------------------------------------------------------------ protocol messages


PROTOCOL_VERSION = "1.0"

# v2 §7.3 core message types. Documented (and used by WP-4); not enforced as a
# closed set so newer protocol versions can add types (v2 §7.5).
MESSAGE_TYPES = frozenset(
    {
        "task.assigned",
        "task.accepted",
        "task.progress",
        "task.blocked",
        "task.input_required",
        "task.approval_required",
        "task.result",
        "task.failed",
        "task.cancelled",
        "review.requested",
        "review.accepted",
        "review.rejected",
        "integration.conflict",
        "verification.started",
        "verification.completed",
    }
)


@dataclass
class ArtifactRef:
    artifact_id: str
    kind: str = ""
    path: str = ""
    sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ArtifactRef":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class EvidenceRef:
    kind: str  # ver | artifact | file | command
    reference: str

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EvidenceRef":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class TaskMessage:
    """Protocol envelope (v2 §7.2). message_id uniqueness is the idempotency
    key: a redelivered message must not cause a second side effect."""

    message_id: str
    root_task_id: str = ""
    task_id: str = ""
    worker_id: str = ""
    attempt: int = 0
    message_type: str = ""
    created_at: str = field(default_factory=utcnow_iso)
    correlation_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    artifacts: list[ArtifactRef] = field(default_factory=list)
    evidence: list[EvidenceRef] = field(default_factory=list)
    protocol_version: str = PROTOCOL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "message_id": self.message_id,
            "root_task_id": self.root_task_id,
            "task_id": self.task_id,
            "worker_id": self.worker_id,
            "attempt": self.attempt,
            "message_type": self.message_type,
            "created_at": self.created_at,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
            "artifacts": [a.to_dict() for a in self.artifacts],
            "evidence": [e.to_dict() for e in self.evidence],
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskMessage":
        return cls(
            message_id=d["message_id"],
            root_task_id=d.get("root_task_id", ""),
            task_id=d.get("task_id", ""),
            worker_id=d.get("worker_id", ""),
            attempt=int(d.get("attempt", 0)),
            message_type=d.get("message_type", ""),
            created_at=d.get("created_at", ""),
            correlation_id=d.get("correlation_id", ""),
            payload=d.get("payload") or {},
            artifacts=[ArtifactRef.from_dict(a) for a in d.get("artifacts") or [] if isinstance(a, dict)],
            evidence=[EvidenceRef.from_dict(e) for e in d.get("evidence") or [] if isinstance(e, dict)],
            protocol_version=d.get("protocol_version", PROTOCOL_VERSION),
        )


# ------------------------------------------------------------------ validation


def validate_task_result(result: dict[str, Any]) -> list[str]:
    """Worker 交付协议 (v2 §7.4): a bare "done" is not a machine-checkable result."""
    problems: list[str] = []
    status = str(result.get("status", "")).strip()
    if status not in ("completed", "failed"):
        problems.append("result.status must be 'completed' or 'failed'")
    if not str(result.get("summary", "")).strip():
        problems.append("result.summary must not be empty")
    for i, item in enumerate(result.get("tests") or []):
        if not isinstance(item, dict) or not str(item.get("command", "")).strip() or not str(item.get("status", "")).strip():
            problems.append(f"tests[{i}] must include command and status")
    for key in ("changed_files", "known_risks", "follow_up"):
        value = result.get(key)
        if value is not None and not isinstance(value, list):
            problems.append(f"result.{key} must be a list")
    return problems


def validate_dependency_graph(specs: list[TaskSpec]) -> list[str]:
    """DAG checks for a set of task specs: duplicate ids, unknown/self
    dependencies, cycles. (DAG *scheduling* itself is WP-3.)"""
    errors: list[str] = []
    ids = [s.task_id for s in specs]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        errors.append(f"duplicate task ids: {', '.join(duplicates)}")
    by_id = {s.task_id: s for s in specs}
    for spec in specs:
        for dep in spec.dependencies:
            if dep == spec.task_id:
                errors.append(f"task {spec.task_id} depends on itself")
            elif dep not in by_id:
                errors.append(f"task {spec.task_id} depends on unknown task {dep}")
    # iterative three-color DFS; only report each cycle edge once
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(by_id, white)
    for start in by_id:
        if color[start] != white:
            continue
        color[start] = gray
        stack: list[tuple[str, Any]] = [(start, iter(by_id[start].dependencies))]
        while stack:
            node, deps_iter = stack[-1]
            advanced = False
            for dep in deps_iter:
                if dep not in by_id or dep == node:
                    continue
                if color[dep] == gray:
                    errors.append(f"dependency cycle detected through task {dep}")
                elif color[dep] == white:
                    color[dep] = gray
                    stack.append((dep, iter(by_id[dep].dependencies)))
                    advanced = True
                    break
            if not advanced:
                color[node] = black
                stack.pop()
    return errors
