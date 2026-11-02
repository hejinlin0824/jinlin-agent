"""SessionState: fold the event log into the state the loop runs on.

The event log is the truth; this module is the (re)projection. `recover()` can
rebuild state from an empty object at any time, which is what makes crash
recovery and auditing cheap.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from .task_contract import transition_session_status

from .domain import (
    Claim,
    Decision,
    Fact,
    Phase,
    Status,
    TaskPlan,
    VerificationRun,
    utcnow_iso,
)


@dataclass
class SessionState:
    session_id: str = ""
    workspace: str = ""
    request: str = ""
    model: str = ""
    config_hash: str = ""
    task_contract: dict[str, Any] = field(default_factory=dict)
    status: str = Status.CREATED.value
    phase: str = Phase.CREATED.value
    turn_index: int = 0
    step_index: int = 0
    last_seq: int = 0
    last_checkpoint_seq: int = 0
    started_at: str = ""
    updated_at: str = ""

    plan: TaskPlan = field(default_factory=TaskPlan)
    facts: list[Fact] = field(default_factory=list)
    decisions: list[Decision] = field(default_factory=list)
    claims: list[Claim] = field(default_factory=list)

    pending_approval: dict[str, Any] | None = None
    approval_count: int = 0

    verification_runs: list[VerificationRun] = field(default_factory=list)
    last_verification_id: str | None = None
    baseline: dict[str, Any] | None = None
    pending_question: dict[str, Any] | None = None
    user_messages: list[str] = field(default_factory=list)

    tool_calls_total: int = 0
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    compactions: int = 0
    consecutive_tool_failures: int = 0
    loop_events: int = 0
    last_signature: str = ""
    signature_streak: int = 0
    last_result_hash: str = ""

    recent_observations: list[dict[str, Any]] = field(default_factory=list)
    recent_assistant: list[dict[str, Any]] = field(default_factory=list)
    last_response_id: str = ""
    last_error: dict[str, Any] | None = None

    workspace_snapshot: dict[str, Any] = field(default_factory=dict)
    sandbox_level: str = "L0"
    config_violations: list[dict[str, Any]] = field(default_factory=list)

    read_hashes: dict[str, str] = field(default_factory=dict)
    written_files: dict[str, dict[str, Any]] = field(default_factory=dict)
    deleted_files: list[str] = field(default_factory=list)

    completion: dict[str, Any] | None = None
    report_artifact_id: str | None = None

    # ------------------------------------------------------------------ helpers

    def is_terminal(self) -> bool:
        return self.status in (
            Status.COMPLETED.value,
            Status.FAILED.value,
            Status.CANCELLED.value,
        )

    def record_observation(self, obs: dict[str, Any], cap: int = 24) -> None:
        self.recent_observations.append(obs)
        if len(self.recent_observations) > cap:
            del self.recent_observations[: len(self.recent_observations) - cap]

    def record_assistant(self, msg: dict[str, Any], cap: int = 8) -> None:
        self.recent_assistant.append(msg)
        if len(self.recent_assistant) > cap:
            del self.recent_assistant[: len(self.recent_assistant) - cap]

    def diff_paths(self) -> list[str]:
        return sorted(set(self.written_files) | set(self.deleted_files))

    def state_hash(self) -> str:
        """Stable hash of the business state (for checkpoint integrity checks)."""
        payload = {
            "status": self.status,
            "phase": self.phase,
            "step": self.step_index,
            "plan": self.plan.to_dict(),
            "written": sorted(self.written_files),
            "seq": self.last_seq,
        }
        return hashlib.sha256(
            repr(sorted(payload.items())).encode("utf-8")
        ).hexdigest()[:16]


# --------------------------------------------------------------------------- fold


def recover(events: Iterable[dict[str, Any]]) -> SessionState:
    state = SessionState()
    for evt in events:
        apply_event(state, evt)
    return state


def apply_event(state: SessionState, evt: dict[str, Any]) -> SessionState:
    etype = evt.get("type", "")
    payload = evt.get("payload") or {}
    seq = evt.get("seq")
    if isinstance(seq, int):
        state.last_seq = max(state.last_seq, seq)
    ts = evt.get("ts")
    if ts:
        state.updated_at = ts

    handler = _HANDLERS.get(etype)
    if handler is not None:
        handler(state, payload)
    return state


def _session_created(s: SessionState, p: dict[str, Any]) -> None:
    s.session_id = p.get("session_id", s.session_id)
    s.workspace = p.get("workspace", s.workspace)
    s.request = p.get("request", s.request)
    s.model = p.get("model", s.model)
    s.config_hash = p.get("config_hash", s.config_hash)
    s.task_contract = dict(p.get("task_contract") or s.task_contract)
    s.started_at = p.get("started_at", s.started_at)
    s.status = transition_session_status(s.status, Status.RUNNING.value)
    s.phase = Phase.INITIALIZING.value


def _workspace_snapshot(s: SessionState, p: dict[str, Any]) -> None:
    s.workspace_snapshot = p.get("snapshot", {})
    if s.phase == Phase.INITIALIZING.value:
        s.phase = Phase.INVESTIGATING.value


def _phase_changed(s: SessionState, p: dict[str, Any]) -> None:
    s.phase = p.get("to", s.phase)


def _step_started(s: SessionState, p: dict[str, Any]) -> None:
    s.step_index = int(p.get("step_index", s.step_index + 1))
    s.status = transition_session_status(s.status, Status.RUNNING.value)


def _context_built(s: SessionState, p: dict[str, Any]) -> None:
    s.tokens_in += int(p.get("tokens_estimate", 0))


def _context_compacted(s: SessionState, p: dict[str, Any]) -> None:
    s.compactions += 1


def _model_requested(s: SessionState, p: dict[str, Any]) -> None:
    s.model_calls += 1


def _model_attempt(s: SessionState, p: dict[str, Any]) -> None:
    usage = p.get("usage") or {}
    s.tokens_in += int(usage.get("prompt_tokens", 0))
    s.tokens_out += int(usage.get("completion_tokens", 0))
    if p.get("error_code"):
        s.last_error = {"code": p["error_code"], "message": p.get("error_message", "")}


def _model_responded(s: SessionState, p: dict[str, Any]) -> None:
    s.last_response_id = p.get("response_id", "")
    s.record_assistant(
        {
            "response_id": p.get("response_id", ""),
            "text": p.get("text", ""),
            "tool_calls": p.get("tool_calls", []),
            "finish_reason": p.get("finish_reason", ""),
        }
    )


def _tool_intent(s: SessionState, p: dict[str, Any]) -> None:
    s.tool_calls_total += 1


def _tool_completed(s: SessionState, p: dict[str, Any]) -> None:
    s.consecutive_tool_failures = 0
    if s.last_error and s.last_error.get("code") in ("TOOL_EXECUTION_FAILED", "TOOL_TIMEOUT", "TOOL_SCHEMA_ERROR", "TOOL_NOT_FOUND", "CMD_ERROR"):
        s.last_error = None
    s.record_observation(
        {
            "tool_call_id": p.get("tool_call_id"),
            "tool": p.get("tool"),
            "ok": True,
            "exit_code": p.get("exit_code"),
            "preview": p.get("output_preview", ""),
            "artifact_id": p.get("artifact_id"),
            "truncated": p.get("truncated"),
        }
    )
    for path, h in (p.get("read_hashes") or {}).items():
        s.read_hashes[path] = h
    for f in p.get("written_files") or []:
        path = f.get("path")
        if path:
            s.written_files[path] = {
                "hash_before": f.get("hash_before"),
                "hash_after": f.get("hash_after"),
                "tool_call_id": p.get("tool_call_id"),
            }
    for path in p.get("deleted_files") or []:
        if path not in s.deleted_files:
            s.deleted_files.append(path)
    sig = p.get("signature")
    if sig:
        if sig == s.last_signature:
            s.signature_streak += 1
        else:
            s.last_signature = sig
            s.signature_streak = 1
        s.last_result_hash = p.get("result_hash", s.last_result_hash)


def _tool_failed(s: SessionState, p: dict[str, Any]) -> None:
    s.consecutive_tool_failures += 1
    s.last_error = p.get("error") or {"code": "TOOL_EXECUTION_FAILED", "message": ""}
    s.record_observation(
        {
            "tool_call_id": p.get("tool_call_id"),
            "tool": p.get("tool"),
            "ok": False,
            "error": s.last_error,
            "preview": "",
            "artifact_id": p.get("artifact_id"),
        }
    )


def _tool_denied(s: SessionState, p: dict[str, Any]) -> None:
    s.consecutive_tool_failures += 1
    s.last_error = p.get("error") or {"code": "APPROVAL_DENIED", "message": ""}
    s.record_observation(
        {
            "tool_call_id": p.get("tool_call_id"),
            "tool": p.get("tool"),
            "ok": False,
            "error": s.last_error,
            "preview": "",
        }
    )


def _approval_requested(s: SessionState, p: dict[str, Any]) -> None:
    s.pending_approval = p.get("approval") or p
    s.status = transition_session_status(s.status, Status.WAITING_APPROVAL.value)


def _approval_resolved(s: SessionState, p: dict[str, Any]) -> None:
    s.pending_approval = None
    s.approval_count += 1
    s.status = transition_session_status(s.status, Status.RUNNING.value)


def _plan_updated(s: SessionState, p: dict[str, Any]) -> None:
    s.plan = TaskPlan.from_dict(p.get("plan") or {})
    if s.phase in (Phase.PLANNING.value, Phase.INVESTIGATING.value, Phase.INITIALIZING.value):
        s.phase = Phase.PLANNING.value


def _fact_recorded(s: SessionState, p: dict[str, Any]) -> None:
    s.facts.append(Fact.from_dict(p.get("fact") or {}))


def _decision_recorded(s: SessionState, p: dict[str, Any]) -> None:
    s.decisions.append(Decision.from_dict(p.get("decision") or {}))


def _verification_started(s: SessionState, p: dict[str, Any]) -> None:
    s.last_verification_id = p.get("verification_id")
    s.phase = Phase.VERIFYING.value


def _verification_completed(s: SessionState, p: dict[str, Any]) -> None:
    run = VerificationRun.from_dict(p.get("verification") or {})
    s.verification_runs.append(run)
    s.last_verification_id = run.verification_id


def _verification_baseline(s: SessionState, p: dict[str, Any]) -> None:
    s.baseline = p.get("baseline")


def _session_waiting_user(s: SessionState, p: dict[str, Any]) -> None:
    s.status = transition_session_status(s.status, Status.WAITING_USER.value)
    s.pending_question = p.get("question") or p


def _session_user_input(s: SessionState, p: dict[str, Any]) -> None:
    text = str(p.get("text") or "").strip()
    if text:
        s.user_messages.append(text)
    s.pending_question = None
    s.status = Status.RUNNING.value


def _completion_proposed(s: SessionState, p: dict[str, Any]) -> None:
    s.claims = [Claim.from_dict(c) for c in p.get("claims", [])]
    s.phase = Phase.FINALIZING.value


def _completion_rejected(s: SessionState, p: dict[str, Any]) -> None:
    s.last_error = {
        "code": "COMPLETION_REJECTED",
        "message": "; ".join(p.get("reasons", [])),
    }
    s.phase = Phase.REPAIRING.value


def _session_completed(s: SessionState, p: dict[str, Any]) -> None:
    s.completion = {
        "result": p.get("result", "completed"),
        "summary": p.get("summary", ""),
    }
    s.report_artifact_id = p.get("report_artifact_id")
    s.status = transition_session_status(s.status, Status.COMPLETED.value)
    s.phase = Phase.COMPLETED.value
    s.last_error = None
    s.pending_question = None


def _session_failed(s: SessionState, p: dict[str, Any]) -> None:
    s.status = transition_session_status(s.status, Status.FAILED.value)
    s.last_error = p.get("error") or {"code": "SESSION_CORRUPTED", "message": ""}


def _session_cancelled(s: SessionState, p: dict[str, Any]) -> None:
    s.status = transition_session_status(s.status, Status.CANCELLED.value)


def _sandbox_mode(s: SessionState, p: dict[str, Any]) -> None:
    s.sandbox_level = p.get("level", "L0")


def _config_escalation_blocked(s: SessionState, p: dict[str, Any]) -> None:
    s.config_violations.append(p)


def _loop_detected(s: SessionState, p: dict[str, Any]) -> None:
    s.loop_events += 1


def _session_checkpoint(s: SessionState, p: dict[str, Any]) -> None:
    s.last_checkpoint_seq = int(p.get("seq", s.last_seq))


_HANDLERS = {
    "session.created": _session_created,
    "session.resumed": lambda s, p: None,
    "session.checkpoint": _session_checkpoint,
    "workspace.snapshot": _workspace_snapshot,
    "phase.changed": _phase_changed,
    "step.started": _step_started,
    "context.built": _context_built,
    "context.compacted": _context_compacted,
    "model.requested": _model_requested,
    "model.attempt": _model_attempt,
    "model.responded": _model_responded,
    "tool.intent": _tool_intent,
    "tool.completed": _tool_completed,
    "tool.failed": _tool_failed,
    "tool.denied": _tool_denied,
    "approval.requested": _approval_requested,
    "approval.resolved": _approval_resolved,
    "plan.updated": _plan_updated,
    "fact.recorded": _fact_recorded,
    "decision.recorded": _decision_recorded,
    "verification.started": _verification_started,
    "verification.completed": _verification_completed,
    "verification.baseline": _verification_baseline,
    "session.waiting_user": _session_waiting_user,
    "session.user_input": _session_user_input,
    "completion.proposed": _completion_proposed,
    "completion.rejected": _completion_rejected,
    "session.completed": _session_completed,
    "session.failed": _session_failed,
    "session.cancelled": _session_cancelled,
    "sandbox.mode": _sandbox_mode,
    "config.escalation_blocked": _config_escalation_blocked,
    "loop.detected": _loop_detected,
}
