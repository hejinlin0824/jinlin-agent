"""Task Protocol (v2.0 WP-V2.0-4; ADR-004).

The protocol layer on top of the Task Ledger: publishing, validation and
at-least-once consumption of task.* / review.* messages, plus the structured
worker deliverable (v2 §7.4) and the reviewer verdict (v2 §12.4).

Guarantees, stated honestly:
  * RECORDING is exactly-once — message_id is the messages table primary key,
    so a redelivered message creates no second row and triggers no second
    side effect (deliver_result is idempotent by construction).
  * PROCESSING is at-least-once — drain() marks a message processed AFTER its
    handler returns; a crash in between causes redelivery, so handlers must
    be idempotent (the ledger dedup above is what makes that practical).
  * VERSIONING — any protocol major matching PROTOCOL_MAJOR is accepted
    (minor versions ride along); an incompatible major is a poison message
    and drain fails closed with TASK_MESSAGE_INVALID instead of guessing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import TASK_MESSAGE_INVALID, TASK_RESULT_INVALID, AgentError
from .ledger import TaskLedger
from .model.base import ModelRequest
from .domain import Message, new_id
from .taskmodel import (
    PROTOCOL_VERSION,
    EvidenceRef,
    TaskMessage,
    TaskSpec,
    new_message_id,
    validate_task_result,
)

PROTOCOL_MAJOR = PROTOCOL_VERSION.split(".")[0]

# review.requested / review.accepted / review.rejected / task.progress are
# produced by this module; the full vocabulary lives in taskmodel.MESSAGE_TYPES.


# --------------------------------------------------------------------------- envelope


def validate_envelope(message: TaskMessage) -> list[str]:
    problems: list[str] = []
    if not str(message.message_id).strip():
        problems.append("message_id must not be empty")
    if not str(message.message_type).strip():
        problems.append("message_type must not be empty")
    if not str(message.root_task_id).strip() and not str(message.task_id).strip():
        problems.append("message must carry root_task_id or task_id")
    version = str(message.protocol_version or "").strip()
    if not version:
        problems.append("protocol_version must not be empty")
    elif version.split(".")[0] != PROTOCOL_MAJOR:
        problems.append(
            f"protocol_version {version!r} is incompatible with this build (major {PROTOCOL_MAJOR})"
        )
    return problems


class MessagePump:
    def __init__(self, ledger: TaskLedger) -> None:
        self.ledger = ledger

    def publish(self, message: TaskMessage) -> bool:
        """Record a message; False for a redelivered message_id (no second row,
        no second side effect downstream). The protocol version rides in the
        payload (no dedicated column), so the version gate survives persistence."""
        payload = dict(message.payload or {})
        payload.setdefault("_protocol_version", message.protocol_version)
        return self.ledger.record_message(
            TaskMessage.from_dict({**message.to_dict(), "payload": payload})
        )

    def drain(
        self,
        root_task_id: str,
        handler: Callable[[TaskMessage], None],
        message_type: str | None = None,
        limit: int | None = None,
    ) -> int:
        """Process each pending message exactly once (at-least-once delivery,
        idempotent handlers). A poison message (invalid envelope/incompatible
        version) fails closed: TASK_MESSAGE_INVALID, nothing guessed."""
        processed = 0
        for row in self.ledger.pending_messages(root_task_id, message_type=message_type, limit=limit):
            message = TaskMessage.from_dict(
                {
                    "message_id": row["message_id"],
                    "root_task_id": row["root_task_id"],
                    "task_id": row["task_id"],
                    "worker_id": row["worker_id"],
                    "attempt": row["attempt"],
                    "message_type": row["message_type"],
                    "created_at": row["created_at"],
                    "correlation_id": row["correlation_id"],
                    "payload": row["payload"],
                    "protocol_version": row["payload"].get("_protocol_version", PROTOCOL_VERSION),
                }
            )
            problems = validate_envelope(message)
            if problems:
                raise AgentError(
                    TASK_MESSAGE_INVALID,
                    f"poison message {message.message_id}: " + "; ".join(problems),
                    source="protocol",
                )
            handler(message)
            self.ledger.mark_message_processed(message.message_id)
            processed += 1
        return processed


# --------------------------------------------------------------------------- worker reporting


def report_progress(
    ledger: TaskLedger,
    spec: TaskSpec,
    worker_id: str,
    attempt: int,
    note: str,
    correlation_id: str = "",
) -> bool:
    return ledger.record_message(
        TaskMessage(
            message_id=new_message_id(),
            root_task_id=spec.root_task_id or spec.task_id,
            task_id=spec.task_id,
            worker_id=worker_id,
            attempt=attempt,
            message_type="task.progress",
            payload={"note": note},
            correlation_id=correlation_id,
        )
    )


def report_blocked(
    ledger: TaskLedger,
    spec: TaskSpec,
    worker_id: str,
    attempt: int,
    reason: str,
    correlation_id: str = "",
) -> bool:
    return ledger.record_message(
        TaskMessage(
            message_id=new_message_id(),
            root_task_id=spec.root_task_id or spec.task_id,
            task_id=spec.task_id,
            worker_id=worker_id,
            attempt=attempt,
            message_type="task.blocked",
            payload={"reason": reason},
            correlation_id=correlation_id,
        )
    )


def deliver_result(ledger: TaskLedger, spec: TaskSpec, worker_id: str, attempt: int, result: dict[str, Any]) -> bool:
    """Publish the structured deliverable (v2 §7.4) and apply it exactly once.
    The message_id is DETERMINISTIC (task, worker, attempt): a redelivered
    result maps to the same envelope id, so the ledger's unique key dedups it
    and record_result never runs twice."""
    problems = validate_task_result(result)
    if problems:
        raise AgentError(TASK_RESULT_INVALID, "invalid task result: " + "; ".join(problems), source="protocol")
    import hashlib

    deterministic_id = "msg_res_" + hashlib.sha256(
        f"{spec.task_id}:{worker_id}:{attempt}".encode("utf-8")
    ).hexdigest()[:16]
    message = TaskMessage(
        message_id=deterministic_id,
        root_task_id=spec.root_task_id or spec.task_id,
        task_id=spec.task_id,
        worker_id=worker_id,
        attempt=attempt,
        message_type="task.result",
        payload={"result": result},
        evidence=[
            EvidenceRef(kind="ver", reference=str(t.get("command", "")))
            for t in result.get("tests", [])
            if isinstance(t, dict)
        ],
    )
    fresh = ledger.record_message(message)
    if fresh:
        ledger.record_result(spec.task_id, result)
    return fresh


# --------------------------------------------------------------------------- reviewer protocol (v2 §12.4)


@dataclass
class ReviewResult:
    status: str  # accepted | rejected
    findings: list[dict[str, Any]] = field(default_factory=list)
    required_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "findings": list(self.findings),
            "required_actions": list(self.required_actions),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ReviewResult":
        return cls(
            status=str(d.get("status", "")).strip(),
            findings=[f for f in d.get("findings") or [] if isinstance(f, dict)],
            required_actions=[str(a) for a in d.get("required_actions") or []],
        )


_SEVERITIES = {"low", "medium", "high"}


def validate_review_result(data: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    status = str(data.get("status", "")).strip()
    if status not in ("accepted", "rejected"):
        problems.append("review status must be 'accepted' or 'rejected'")
    for i, finding in enumerate(data.get("findings") or []):
        if not isinstance(finding, dict) or str(finding.get("severity", "")).lower() not in _SEVERITIES:
            problems.append(f"findings[{i}].severity must be one of low/medium/high")
    if status == "rejected" and not [a for a in data.get("required_actions") or [] if str(a).strip()]:
        problems.append("a rejected review must list required_actions")
    return problems


REVIEWER_SYSTEM = """You are an independent reviewer of a completed engineering subtask. You inspect the
deliverable (summary, changed files, verification evidence) against its acceptance criteria.

Output ONLY a JSON object:
{"status": "accepted" | "rejected",
 "findings": [{"severity": "low|medium|high", "file": "...", "line": 0, "description": "...", "evidence": "..."}],
 "required_actions": ["..."]}
Reject when evidence is missing, claims exceed the verification, or acceptance
criteria are unmet; a rejection MUST list concrete required_actions."""


def conduct_review(provider: Any, task_title: str, result: dict[str, Any], evidence_summary: str) -> ReviewResult:
    """Model-driven review (scripted in offline tests). Malformed verdicts raise
    MODEL_INVALID_RESPONSE — a reviewer that cannot produce a verdict must not
    be treated as an acceptance."""
    request = ModelRequest(
        request_id=new_id("req"),
        messages=[
            Message(role="system", content=REVIEWER_SYSTEM),
            Message(
                role="user",
                content=(
                    f"Task: {task_title}\n\n"
                    "Deliverable:\n"
                    + json.dumps(result, ensure_ascii=False)
                    + "\n\nVerification evidence:\n"
                    + (evidence_summary or "(none)")
                ),
            ),
        ],
        tools=[],
        temperature=0.0,
        max_output_tokens=2048,
        stream=False,
        timeout_seconds=120,
    )
    response = provider.complete(request)
    raw = (response.text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    try:
        data = json.loads(raw[raw.find("{") : raw.rfind("}") + 1])
    except (json.JSONDecodeError, ValueError) as exc:
        raise AgentError("MODEL_INVALID_RESPONSE", f"reviewer output is not valid JSON: {exc}", source="protocol") from exc
    problems = validate_review_result(data)
    if problems:
        raise AgentError("MODEL_INVALID_RESPONSE", "invalid review verdict: " + "; ".join(problems), source="protocol")
    return ReviewResult.from_dict(data)


def request_review(ledger: TaskLedger, spec: TaskSpec, worker_id: str, correlation_id: str = "") -> TaskMessage:
    message = TaskMessage(
        message_id=new_message_id(),
        root_task_id=spec.root_task_id or spec.task_id,
        task_id=spec.task_id,
        worker_id=worker_id,
        message_type="review.requested",
        payload={"note": "deliverable completed; independent review requested"},
        correlation_id=correlation_id,
    )
    ledger.record_message(message)
    return message


def record_review(ledger: TaskLedger, spec: TaskSpec, worker_id: str, review: ReviewResult, correlation_id: str = "") -> TaskMessage:
    problems = validate_review_result(review.to_dict())
    if problems:
        raise AgentError(TASK_MESSAGE_INVALID, "invalid review verdict: " + "; ".join(problems), source="protocol")
    message = TaskMessage(
        message_id=new_message_id(),
        root_task_id=spec.root_task_id or spec.task_id,
        task_id=spec.task_id,
        worker_id=worker_id,
        message_type="review.accepted" if review.status == "accepted" else "review.rejected",
        payload={"review": review.to_dict()},
        correlation_id=correlation_id,
    )
    ledger.record_message(message)
    return message
