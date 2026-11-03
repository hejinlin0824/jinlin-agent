"""Domain model: messages, tool calls, plan, facts, verification, claims.

Everything here is serializable to plain JSON so it can live in events and
artifacts. No framework types leak in.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# --------------------------------------------------------------------------- ids


def new_id(prefix: str) -> str:
    """Short, sortable-ish id: prefix_<hex12>."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def utcnow_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ------------------------------------------------------------------ enums


class Phase(str, Enum):
    CREATED = "CREATED"
    INITIALIZING = "INITIALIZING"
    INVESTIGATING = "INVESTIGATING"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    VERIFYING = "VERIFYING"
    REPAIRING = "REPAIRING"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"


class Status(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    WAITING_USER = "waiting_user"
    PAUSED = "paused"
    FAILED = "failed"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = {Status.COMPLETED, Status.FAILED, Status.CANCELLED}


class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"
    FAILED = "failed"


class SideEffect(str, Enum):
    NONE = "NONE"
    READ = "READ"
    WRITE = "WRITE"
    EXECUTE = "EXECUTE"
    NETWORK = "NETWORK"
    DESTRUCTIVE = "DESTRUCTIVE"
    EXTERNAL_SIDE_EFFECT = "EXTERNAL_SIDE_EFFECT"


class RiskLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    DESTRUCTIVE = "destructive"


class ApprovalDecision(str, Enum):
    APPROVED_ONCE = "approved_once"
    APPROVED_FOR_SESSION = "approved_for_session"
    DENIED = "denied"
    MODIFIED = "modified"
    EXPIRED = "expired"


class VerificationLevel(str, Enum):
    L0_DIFF = "L0"
    L1_STRUCTURE = "L1"
    L2_STATIC = "L2"
    L3_TESTS = "L3"
    L4_BEHAVIOR = "L4"
    L5_REGRESSION = "L5"


class CompletionResult(str, Enum):
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    PARTIAL = "partial"
    BLOCKED = "blocked"
    FAILED = "failed"


# ------------------------------------------------------------------ messages


@dataclass
class ToolCallRequest:
    id: str
    name: str
    arguments: str = ""  # raw JSON string as emitted by the model
    parsed: dict[str, Any] | None = None
    parse_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": self.arguments,
            "parsed": self.parsed,
            "parse_error": self.parse_error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ToolCallRequest":
        return cls(
            id=d["id"],
            name=d["name"],
            arguments=d.get("arguments", ""),
            parsed=d.get("parsed"),
            parse_error=d.get("parse_error"),
        )


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d

    def to_api(self) -> dict[str, Any]:
        """OpenAI-compatible chat message shape."""
        d: dict[str, Any] = {"role": self.role, "content": self.content or (None if self.tool_calls else "")}
        if self.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.name, "arguments": tc.arguments},
                }
                for tc in self.tool_calls
            ]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        if self.name:
            d["name"] = self.name
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Message":
        return cls(
            role=d["role"],
            content=d.get("content") or "",
            tool_calls=[ToolCallRequest.from_dict(x) for x in d.get("tool_calls", [])],
            tool_call_id=d.get("tool_call_id"),
            name=d.get("name"),
        )


@dataclass
class TokenUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class ModelResponse:
    response_id: str
    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw_artifact_id: str | None = None
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "response_id": self.response_id,
            "text": self.text,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "finish_reason": self.finish_reason,
            "usage": self.usage.to_dict(),
            "raw_artifact_id": self.raw_artifact_id,
            "model": self.model,
        }


# ------------------------------------------------------------------ plan/memory


@dataclass
class PlanStep:
    step_id: str
    description: str
    status: str = StepStatus.PENDING.value
    evidence: list[str] = field(default_factory=list)
    blocked_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "description": self.description,
            "status": self.status,
            "evidence": list(self.evidence),
            "blocked_reason": self.blocked_reason,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PlanStep":
        return cls(
            step_id=d["step_id"],
            description=d.get("description", ""),
            status=d.get("status", StepStatus.PENDING.value),
            evidence=list(d.get("evidence", [])),
            blocked_reason=d.get("blocked_reason"),
        )


@dataclass
class TaskPlan:
    objective: str = ""
    assumptions: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    steps: list[PlanStep] = field(default_factory=list)
    verification_strategy: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    version: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "objective": self.objective,
            "assumptions": list(self.assumptions),
            "constraints": list(self.constraints),
            "steps": [s.to_dict() for s in self.steps],
            "verification_strategy": list(self.verification_strategy),
            "risks": list(self.risks),
            "version": self.version,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TaskPlan":
        return cls(
            objective=d.get("objective", ""),
            assumptions=list(d.get("assumptions", [])),
            constraints=list(d.get("constraints", [])),
            steps=[PlanStep.from_dict(x) for x in d.get("steps", [])],
            verification_strategy=list(d.get("verification_strategy", [])),
            risks=list(d.get("risks", [])),
            version=d.get("version", 0),
        )

    def is_effective(self) -> bool:
        return bool(self.objective.strip()) and len(self.steps) > 0

    def open_steps(self) -> list[PlanStep]:
        return [s for s in self.steps if s.status in (StepStatus.PENDING.value, StepStatus.IN_PROGRESS.value)]


@dataclass
class Fact:
    fact_id: str
    statement: str
    source_type: str = "tool"
    source_reference: str = ""
    confidence: str = "confirmed"  # confirmed | likely | assumed
    created_at_step: int = 0
    invalidated_at_step: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Fact":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Decision:
    decision_id: str
    question: str
    selected_option: str
    alternatives: list[str] = field(default_factory=list)
    reason: str = ""
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Decision":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# ------------------------------------------------------------------ verification


@dataclass
class VerificationRun:
    verification_id: str
    level: str
    type: str
    command: list[str] | None = None
    cwd: str | None = None
    started_at: str = ""
    completed_at: str | None = None
    exit_code: int | None = None
    status: str = "running"  # running | passed | failed | skipped | error
    output_artifact_id: str | None = None
    findings: list[str] = field(default_factory=list)
    reason_skipped: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)
    sandbox_level: str = ""  # v2 §10.3 evidence: which sandbox ran it

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VerificationRun":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Claim:
    claim_id: str
    statement: str
    evidence_refs: list[str] = field(default_factory=list)
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Claim":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


# ------------------------------------------------------------------ helpers


def dumps_compact(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True
