"""Tool dispatch: the single execution pipeline.

Order is fixed and every step is observable:
  exists -> parse args -> schema validation -> policy -> approval -> intent event
  -> handler -> normalize/truncate -> artifact -> completed/failed event -> loop
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .domain import SideEffect, ToolCallRequest, dumps_compact, new_id
from .errors import (
    APPROVAL_DENIED,
    TOOL_NOT_FOUND,
    TOOL_SCHEMA_ERROR,
    AgentError,
)
from .policy import PolicyEngine
from .tools.base import ToolContext, ToolResult
from .tools.registry import ToolRegistry
from .tools.schema import validate as validate_schema

_SENSITIVE = ("api_key", "apikey", "token", "secret", "password", "authorization")


@dataclass
class DispatchResult:
    result: ToolResult
    tool: str = ""
    call_id: str = ""
    completion: dict[str, Any] | None = None
    user_input: dict[str, Any] | None = None
    events: list[tuple[str, dict[str, Any]]] = field(default_factory=list)


def redact_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        low = key.lower()
        if any(s in low for s in _SENSITIVE):
            out[key] = "***"
        elif isinstance(value, str):
            out[key] = value if len(value) <= 400 else value[:400] + f"... ({len(value)} chars)"
        else:
            out[key] = value
    return out


def args_hash(args: dict[str, Any]) -> str:
    try:
        return hashlib.sha256(dumps_compact(args).encode("utf-8")).hexdigest()[:16]
    except Exception:
        return hashlib.sha256(repr(args).encode("utf-8")).hexdigest()[:16]


class ToolDispatcher:
    def __init__(
        self,
        registry: ToolRegistry,
        policy: PolicyEngine,
        artifacts: Any,
        emit: Callable[[str, dict[str, Any], str, str], dict[str, Any]],
        ctx: ToolContext,
        config: Any,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.artifacts = artifacts
        self.emit = emit
        self.ctx = ctx
        self.config = config

    # ------------------------------------------------------------------ api

    def execute(self, call: ToolCallRequest, turn_id: str = "", step_id: str = "") -> DispatchResult:
        spec = self.registry.get(call.name)
        started = time.monotonic()

        def elapsed_ms() -> int:
            return int((time.monotonic() - started) * 1000)

        if spec is None:
            error = AgentError(
                TOOL_NOT_FOUND,
                f"unknown tool {call.name!r}; available: {', '.join(self.registry.names())}",
                source="dispatch",
            )
            self._failed(call, error, turn_id, step_id)
            return DispatchResult(result=ToolResult(ok=False, error=error), tool=call.name, call_id=call.id)

        if call.parsed is None:
            error = AgentError(
                TOOL_SCHEMA_ERROR,
                f"arguments for {call.name} are not valid JSON: {call.parse_error or call.arguments[:200]}",
                source="dispatch",
            )
            self._failed(call, error, turn_id, step_id)
            return DispatchResult(result=ToolResult(ok=False, error=error), tool=call.name, call_id=call.id)

        errors = validate_schema(spec.schema, call.parsed)
        if errors:
            error = AgentError(
                TOOL_SCHEMA_ERROR,
                f"invalid arguments for {call.name}: " + "; ".join(errors[:6]),
                source="dispatch",
                retriable=True,
            )
            self._failed(call, error, turn_id, step_id)
            return DispatchResult(result=ToolResult(ok=False, error=error), tool=call.name, call_id=call.id)

        decision = self.policy.evaluate(call.name, spec.side_effect, call.parsed, call.id)
        if not decision.allowed:
            error = decision.error or AgentError(APPROVAL_DENIED, decision.reason, source="policy")
            self._denied(call, error, decision.risk, turn_id, step_id)
            return DispatchResult(result=ToolResult(ok=False, error=error), tool=call.name, call_id=call.id)

        if decision.needs_approval and decision.approval_request is not None:
            approved, note = self._ask_approval(call, decision, turn_id, step_id)
            if not approved:
                error = AgentError(
                    APPROVAL_DENIED,
                    f"operation denied{(' (' + note + ')') if note else ''}: {decision.reason}",
                    source="approval",
                )
                self._denied(call, error, decision.risk, turn_id, step_id)
                return DispatchResult(result=ToolResult(ok=False, error=error), tool=call.name, call_id=call.id)

        payload_hash = args_hash(call.parsed)
        self.emit(
            "tool.intent",
            {
                "tool_call_id": call.id,
                "tool": call.name,
                "args_hash": payload_hash,
                "idempotency_key": hashlib.sha256(f"{call.name}:{payload_hash}".encode()).hexdigest()[:24],
                "args": redact_args(call.parsed),
                "side_effect": spec.side_effect,
                "risk": decision.risk,
                "engine": getattr(self.ctx.sandbox, "engine", "none"),
            },
            turn_id,
            step_id,
        )

        try:
            result = spec.handler(self.ctx, call.parsed)
        except AgentError as exc:
            result = ToolResult(ok=False, error=exc, output=exc.message)
        except Exception as exc:  # never let a tool crash the loop
            result = ToolResult(
                ok=False,
                error=AgentError("TOOL_EXECUTION_FAILED", f"{type(exc).__name__}: {exc}", source=call.name),
                output=f"tool crashed: {type(exc).__name__}: {exc}",
            )

        output = result.output or (result.error.message if result.error else "")
        preview, truncated = self._persist(call, spec, result, output)
        self.ctx.log(f"[tool] {call.name} {'ok' if result.ok else 'fail'} ({elapsed_ms()}ms)")

        if result.ok:
            event_payload = {
                "tool_call_id": call.id,
                "tool": call.name,
                "status": "ok",
                "arguments_hash": payload_hash,
                "signature": f"{call.name}:{payload_hash}",
                "result_hash": hashlib.sha256(preview.encode("utf-8", "replace")).hexdigest()[:16],
                "duration_ms": elapsed_ms(),
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "output_preview": preview,
                "artifact_id": getattr(result, "_artifact_id", None),
                "truncated": truncated,
                "written_files": result.written_files,
                "deleted_files": result.deleted_files,
                "read_hashes": result.read_hashes,
                "extra": _small(result.extra),
            }
            self.emit("tool.completed", event_payload, turn_id, step_id)
            events: list[tuple[str, dict[str, Any]]] = []
            extra = result.extra or {}
            if extra.get("plan_event"):
                self.emit("plan.updated", {"plan": extra["plan_event"], "reason": extra.get("reason", "")}, turn_id, step_id)
            if extra.get("fact_event"):
                self.emit("fact.recorded", {"fact": extra["fact_event"]}, turn_id, step_id)
            if extra.get("decision_event"):
                self.emit("decision.recorded", {"decision": extra["decision_event"]}, turn_id, step_id)
            completion = extra.get("completion")
            if completion:
                self.emit("completion.proposed", {"claims": completion.get("claims", []), "summary": completion.get("summary", "")}, turn_id, step_id)
            return DispatchResult(
                result=result,
                tool=call.name,
                call_id=call.id,
                completion=completion,
                user_input=extra.get("request_user_input"),
                events=events,
            )

        error = result.error or AgentError("TOOL_EXECUTION_FAILED", "tool failed", source=call.name)
        self.emit(
            "tool.failed",
            {
                "tool_call_id": call.id,
                "tool": call.name,
                "arguments_hash": payload_hash,
                "signature": f"{call.name}:{payload_hash}",
                "duration_ms": elapsed_ms(),
                "error": error.to_dict(),
                "output_preview": preview,
                "artifact_id": getattr(result, "_artifact_id", None),
            },
            turn_id,
            step_id,
        )
        return DispatchResult(result=result, tool=call.name, call_id=call.id)

    # ------------------------------------------------------------------ helpers

    def _persist(self, call: ToolCallRequest, spec: Any, result: ToolResult, output: str) -> tuple[str, dict[str, Any] | None]:
        limit = int(getattr(spec, "max_output_chars", 20_000))
        full = result.artifact_content if result.artifact_content is not None else output
        truncated = len(full) > limit or result.artifact_content is not None or result.timed_out
        if not truncated:
            return output, None
        artifact = self.artifacts.write(
            "tool_output",
            full,
            meta={"tool": call.name, "tool_call_id": call.id},
        )
        setattr(result, "_artifact_id", artifact.artifact_id)
        preview = output[:limit]
        meta = {
            "truncated": True,
            "original_characters": len(full),
            "returned_characters": len(preview),
            "artifact_id": artifact.artifact_id,
        }
        if result.timed_out:
            meta["timed_out"] = True
        marker = (
            f"\n\n[output truncated: {len(full)} chars total, {len(preview)} shown. "
            f"full output artifact_id={artifact.artifact_id}; use get_file_info/read_file with the "
            f"artifact path only if strictly needed]"
        )
        if result.extra is None:
            result.extra = {}
        result.extra["truncated"] = meta
        return preview + marker, meta

    def _failed(self, call: ToolCallRequest, error: AgentError, turn_id: str, step_id: str) -> None:
        self.emit(
            "tool.failed",
            {
                "tool_call_id": call.id,
                "tool": call.name,
                "error": error.to_dict(),
                "duration_ms": 0,
                "signature": f"{call.name}:{args_hash(call.parsed or {})}",
            },
            turn_id,
            step_id,
        )

    def _denied(self, call: ToolCallRequest, error: AgentError, risk: str, turn_id: str, step_id: str) -> None:
        self.emit(
            "tool.denied",
            {
                "tool_call_id": call.id,
                "tool": call.name,
                "risk": risk,
                "error": error.to_dict(),
                "signature": f"{call.name}:{args_hash(call.parsed or {})}",
            },
            turn_id,
            step_id,
        )

    def _ask_approval(self, call: ToolCallRequest, decision: Any, turn_id: str, step_id: str) -> tuple[bool, str]:
        from .domain import ApprovalDecision

        request = decision.approval_request
        request.approval_id = new_id("appr")
        request.tool_call_id = call.id
        self.emit("approval.requested", {"approval": request.to_dict()}, turn_id, step_id)
        verdict, note = self.policy.approvals.decide(request, decision.category)
        self.emit(
            "approval.resolved",
            {"approval_id": request.approval_id, "decision": verdict, "note": note, "tool_call_id": call.id},
            turn_id,
            step_id,
        )
        return verdict in (
            ApprovalDecision.APPROVED_ONCE.value,
            ApprovalDecision.APPROVED_FOR_SESSION.value,
            ApprovalDecision.MODIFIED.value,
        ), note


def _small(extra: dict[str, Any] | None) -> dict[str, Any]:
    if not extra:
        return {}
    out: dict[str, Any] = {}
    for key, value in extra.items():
        if key in ("plan_event", "fact_event", "decision_event", "completion"):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value if not isinstance(value, str) or len(value) < 500 else value[:500]
        else:
            out[key] = json.loads(json.dumps(value, default=str)[:2000])
    return out
