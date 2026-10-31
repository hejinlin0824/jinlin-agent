"""Task-state tools: plan, facts, decisions, completion proposal, user input.

These do not touch the filesystem; they mutate session state through events the
dispatcher writes. The plan is runtime state, not display text: completion and
the completion gate both read it.
"""

from __future__ import annotations

import json
from typing import Any

from ..domain import (
    Claim,
    Decision,
    Fact,
    PlanStep,
    SideEffect,
    StepStatus,
    TaskPlan,
    new_id,
)
from ..errors import TOOL_SCHEMA_ERROR, AgentError
from .base import ToolContext, ToolResult, ToolSpec
from .registry import ToolRegistry

_STATUSES = {s.value for s in StepStatus}
_MAX_STEPS = 25


def update_plan(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    raw = args.get("plan") or {}
    if not isinstance(raw, dict):
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, "plan must be an object", source="task"))
    previous: TaskPlan = getattr(ctx.state, "plan", TaskPlan()) if ctx.state else TaskPlan()
    objective = str(raw.get("objective") or previous.objective or "").strip()
    steps_raw = raw.get("steps")
    if steps_raw is None:
        steps_raw = [s.to_dict() for s in previous.steps]
    if not isinstance(steps_raw, list) or not steps_raw:
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, "plan.steps must be a non-empty array", source="task"))
    if len(steps_raw) > _MAX_STEPS:
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, f"at most {_MAX_STEPS} steps", source="task"))
    steps: list[PlanStep] = []
    in_progress = 0
    for i, item in enumerate(steps_raw):
        if isinstance(item, str):
            item = {"description": item}
        if not isinstance(item, dict):
            return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, f"step {i + 1} must be an object", source="task"))
        desc = str(item.get("description") or "").strip()
        if not desc:
            return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, f"step {i + 1} needs a description", source="task"))
        status = item.get("status") or StepStatus.PENDING.value
        if status not in _STATUSES:
            return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, f"step {i + 1}: bad status {status!r}", source="task"))
        evidence = [str(x) for x in (item.get("evidence") or [])]
        if status == StepStatus.COMPLETED.value and not evidence:
            return ToolResult(
                ok=False,
                error=AgentError(
                    "PLAN_EVIDENCE_REQUIRED",
                    f"step {i + 1} cannot be completed without evidence (file path, test run, artifact id)",
                    source="task",
                ),
            )
        if status == StepStatus.SKIPPED.value and not item.get("blocked_reason"):
            return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, f"step {i + 1}: skipped needs a reason", source="task"))
        if status == StepStatus.IN_PROGRESS.value:
            in_progress += 1
        steps.append(
            PlanStep(
                step_id=item.get("step_id") or f"step_{i + 1}",
                description=desc,
                status=status,
                evidence=evidence,
                blocked_reason=item.get("blocked_reason"),
            )
        )
    if in_progress > 1:
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, "at most one step may be in_progress", source="task"))
    plan = TaskPlan(
        objective=objective,
        assumptions=[str(x) for x in (raw.get("assumptions") or previous.assumptions)],
        constraints=[str(x) for x in (raw.get("constraints") or previous.constraints)],
        steps=steps,
        verification_strategy=[str(x) for x in (raw.get("verification_strategy") or previous.verification_strategy)],
        risks=[str(x) for x in (raw.get("risks") or previous.risks)],
        version=previous.version + 1,
    )
    if not plan.objective:
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, "plan.objective is required", source="task"))
    lines = [f"plan v{plan.version}: {plan.objective}"]
    for s in plan.steps:
        mark = {"completed": "[x]", "in_progress": "[>]", "blocked": "[!]", "skipped": "[-]", "failed": "[X]"}.get(s.status, "[ ]")
        lines.append(f"  {mark} {s.step_id}: {s.description}")
    return ToolResult(
        output="\n".join(lines),
        extra={"plan_event": plan.to_dict(), "reason": str(args.get("reason") or "")},
    )


def record_fact(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    statement = str(args.get("statement") or "").strip()
    if not statement:
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, "statement is required", source="task"))
    confidence = args.get("confidence") or "confirmed"
    if confidence not in ("confirmed", "likely", "assumed"):
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, "confidence must be confirmed|likely|assumed", source="task"))
    step = int(getattr(ctx.state, "step_index", 0)) if ctx.state else 0
    fact = Fact(
        fact_id=new_id("fact"),
        statement=statement,
        source_type=str(args.get("source_type") or "tool"),
        source_reference=str(args.get("source_reference") or ""),
        confidence=confidence,
        created_at_step=step,
    )
    return ToolResult(output=f"recorded fact {fact.fact_id}: {statement}", extra={"fact_event": fact.to_dict()})


def record_decision(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    question = str(args.get("question") or "").strip()
    selected = str(args.get("selected_option") or "").strip()
    if not question or not selected:
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, "question and selected_option are required", source="task"))
    decision = Decision(
        decision_id=new_id("dec"),
        question=question,
        selected_option=selected,
        alternatives=[str(x) for x in (args.get("alternatives") or [])],
        reason=str(args.get("reason") or ""),
        evidence=[str(x) for x in (args.get("evidence") or [])],
    )
    return ToolResult(output=f"recorded decision {decision.decision_id}: {selected}", extra={"decision_event": decision.to_dict()})


def propose_completion(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    claims_raw = args.get("claims") or []
    if not isinstance(claims_raw, list):
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, "claims must be an array", source="task"))
    claims: list[dict[str, Any]] = []
    for i, item in enumerate(claims_raw):
        if not isinstance(item, dict) or not str(item.get("statement") or "").strip():
            return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, f"claim {i + 1} needs a statement", source="task"))
        claims.append(
            Claim(
                claim_id=item.get("claim_id") or f"claim_{i + 1}",
                statement=str(item["statement"]),
                evidence_refs=[str(x) for x in (item.get("evidence_refs") or [])],
            ).to_dict()
        )
    if not claims:
        return ToolResult(
            ok=False,
            error=AgentError(
                TOOL_SCHEMA_ERROR,
                "propose_completion requires at least one claim; every finished task leaves evidence",
                source="task",
            ),
        )
    summary = str(args.get("summary") or "")
    return ToolResult(
        output=f"completion proposed with {len(claims)} claim(s); the harness gate decides.",
        extra={"completion": {"claims": claims, "summary": summary}},
    )


def request_user_input(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    question = str(args.get("question") or "").strip()
    if not question:
        return ToolResult(ok=False, error=AgentError(TOOL_SCHEMA_ERROR, "question is required", source="task"))
    options = [str(x) for x in (args.get("options") or [])]
    body = "question for the user: " + question
    if options:
        body += "\noptions: " + " | ".join(options)
    return ToolResult(output=body, extra={"request_user_input": {"question": question, "options": options}})


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(
        ToolSpec(
            name="update_plan",
            description=(
                "Create or update the task plan. The plan is runtime state: completion requires all steps "
                "completed (with evidence) or explicitly skipped with a reason. Keep steps concrete and "
                "small; at most one step in_progress."
            ),
            schema={
                "type": "object",
                "properties": {
                    "plan": {
                        "type": "object",
                        "properties": {
                            "objective": {"type": "string"},
                            "assumptions": {"type": "array", "items": {"type": "string"}},
                            "constraints": {"type": "array", "items": {"type": "string"}},
                            "verification_strategy": {"type": "array", "items": {"type": "string"}},
                            "risks": {"type": "array", "items": {"type": "string"}},
                            "steps": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "step_id": {"type": "string"},
                                        "description": {"type": "string"},
                                        "status": {
                                            "type": "string",
                                            "enum": ["pending", "in_progress", "completed", "blocked", "skipped", "failed"],
                                        },
                                        "evidence": {"type": "array", "items": {"type": "string"}},
                                        "blocked_reason": {"type": "string"},
                                    },
                                    "required": ["description"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "additionalProperties": False,
                    },
                    "reason": {"type": "string"},
                },
                "required": ["plan"],
                "additionalProperties": False,
            },
            handler=update_plan,
            side_effect=SideEffect.NONE.value,
            idempotent=False,
            group="task",
        )
    )
    registry.register(
        ToolSpec(
            name="record_fact",
            description=(
                "Record a verified task fact (e.g. 'project uses pytest', source pyproject.toml) so it "
                "survives context compaction. Do not record guesses as confirmed."
            ),
            schema={
                "type": "object",
                "properties": {
                    "statement": {"type": "string"},
                    "source_type": {"type": "string", "enum": ["tool", "file", "test", "user", "inference"]},
                    "source_reference": {"type": "string"},
                    "confidence": {"type": "string", "enum": ["confirmed", "likely", "assumed"]},
                },
                "required": ["statement"],
                "additionalProperties": False,
            },
            handler=record_fact,
            side_effect=SideEffect.NONE.value,
            idempotent=True,
            group="task",
        )
    )
    registry.register(
        ToolSpec(
            name="record_decision",
            description="Record a design decision with its alternatives and evidence, so later steps and the final report stay consistent.",
            schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "selected_option": {"type": "string"},
                    "alternatives": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                    "evidence": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question", "selected_option"],
                "additionalProperties": False,
            },
            handler=record_decision,
            side_effect=SideEffect.NONE.value,
            idempotent=True,
            group="task",
        )
    )
    registry.register(
        ToolSpec(
            name="propose_completion",
            description=(
                "Propose that the task is done. This does not complete the session: the harness runs the "
                "completion gate against the real workspace, diff, verification runs and every claim's "
                "evidence. If anything fails the task continues. evidence_refs must anchor to something "
                "real: a repository-relative file path, an artifact id, a verification id (ver_...), a "
                "verification level+type (e.g. 'L3 tests'), or the verification command you ran."
            ),
            schema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "claims": {
                        "type": "array",
                        "minLength": 1,
                        "items": {
                            "type": "object",
                            "properties": {
                                "claim_id": {"type": "string"},
                                "statement": {"type": "string"},
                                "evidence_refs": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["statement"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["claims"],
                "additionalProperties": False,
            },
            handler=propose_completion,
            side_effect=SideEffect.NONE.value,
            idempotent=True,
            group="task",
        )
    )
    registry.register(
        ToolSpec(
            name="request_user_input",
            description=(
                "Ask the user a question when a real decision or missing credential blocks progress. "
                "The session pauses and can be resumed. Do not use this for things you can check yourself."
            ),
            schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["question"],
                "additionalProperties": False,
            },
            handler=request_user_input,
            side_effect=SideEffect.NONE.value,
            idempotent=True,
            group="task",
        )
    )
    return registry
