"""Context construction (T3).

The model never sees "everything so far". Every round the harness decides what
enters the window, scores candidates against the current task and observations,
budgets them, and records *why* each block was included or dropped. When the
window fills up, the old tail is replaced by a state digest (never by a silent
size-based cut).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .domain import Message, dumps_compact
from .prompt import wrap_untrusted
from .tokens import estimate_tokens

_SRC_WEIGHT = {
    "task": 100,
    "plan": 90,
    "verification": 85,
    "diff": 80,
    "failure": 78,
    "file": 60,
    "project_map": 40,
    "facts": 55,
}


@dataclass
class ContextBlock:
    block_id: str
    source: str
    content: str
    required: bool = False
    priority: int = 0
    score: float = 0.0
    reason: str = ""
    tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.block_id,
            "source": self.source,
            "required": self.required,
            "tokens": self.tokens,
            "score": round(self.score, 2),
            "reason": self.reason,
        }


@dataclass
class ContextBundle:
    messages: list[Message]
    history_kept: list[Message]
    tokens_estimate: int = 0
    blocks: list[ContextBlock] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    compacted: bool = False
    digest: str = ""
    messages_artifact_id: str | None = None

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "tokens_estimate": self.tokens_estimate,
            "blocks": [b.to_dict() for b in self.blocks],
            "dropped": self.dropped[:20],
            "dropped_count": len(self.dropped),
            "compacted": self.compacted,
            "messages_artifact_id": self.messages_artifact_id,
        }


class ContextBuilder:
    def __init__(
        self,
        config: Any,
        capabilities: Any,
        artifacts: Any,
        sandbox: Any,
        project_instructions: str = "",
        role_instruction: str = "",
        providers: list[Any] | None = None,
    ) -> None:
        self.config = config
        self.capabilities = capabilities
        self.artifacts = artifacts
        self.sandbox = sandbox
        self.project_instructions = project_instructions
        self.role_instruction = role_instruction
        # ContextProviders (v2 §8.2): extra scored blocks; a failing provider
        # is recorded as a drop reason, never silently swallowed
        self.providers = list(providers or [])
        self.ctx_cfg = config.context

    # ------------------------------------------------------------------ api

    def build(self, state: Any, history: list[Message], user_request: str) -> ContextBundle:
        window = int(getattr(self.capabilities, "context_window", 131072))
        reserve = int(self.ctx_cfg.get("output_reserve_tokens", 8192))
        budget = max(2048, window - reserve)
        soft = float(self.ctx_cfg.get("soft_threshold", 0.70)) * budget
        hard = float(self.ctx_cfg.get("hard_threshold", 0.85)) * budget

        system_text = self._system_text(state)
        frame_blocks = self._frame_blocks(state)
        retrieval = self._retrieval_blocks(state, budget)
        for provider in self.providers:
            try:
                retrieval.extend(provider.blocks(state, budget))
            except Exception as exc:
                dropped.append(
                    {
                        "id": f"provider:{type(provider).__name__}",
                        "source": "provider",
                        "tokens": 0,
                        "reason": f"provider failed: {type(exc).__name__}: {exc}",
                    }
                )

        # budget: required first, then scored optional candidates
        blocks: list[ContextBlock] = []
        used = estimate_tokens(system_text) + estimate_tokens(user_request)
        dropped: list[dict[str, Any]] = []

        for block in frame_blocks:
            block.tokens = estimate_tokens(block.content)
            if block.required or used + block.tokens <= budget:
                blocks.append(block)
                used += block.tokens
            else:
                dropped.append({"id": block.block_id, "source": block.source, "tokens": block.tokens, "reason": "budget"})

        optional = sorted(retrieval, key=lambda b: (-b.score, b.block_id))
        retrieval_budget = min(int(self.ctx_cfg.get("max_retrieval_tokens", 40000)), max(0, budget - used))
        retrieval_used = 0
        for block in optional:
            block.tokens = estimate_tokens(block.content)
            if retrieval_used + block.tokens <= retrieval_budget and used + block.tokens <= budget:
                blocks.append(block)
                retrieval_used += block.tokens
                used += block.tokens
            else:
                dropped.append({"id": block.block_id, "source": block.source, "tokens": block.tokens, "reason": "budget"})

        # history handling / compaction
        kept_history, digest, compacted, hist_dropped = self._fit_history(
            history, used, soft, hard, budget
        )
        dropped.extend(hist_dropped)
        if digest:
            used += estimate_tokens(digest)

        dynamic_text = self._render_frame(blocks, state)
        messages: list[Message] = [
            Message(role="system", content=system_text),
            Message(role="user", content=user_request),
        ]
        if digest:
            messages.append(Message(role="user", content=digest))
        messages.extend(kept_history)
        messages.append(Message(role="user", content=dynamic_text))

        artifact = self.artifacts.write_json(
            "context_messages",
            [m.to_dict() for m in messages],
            meta={"session": getattr(state, "session_id", ""), "step": getattr(state, "step_index", 0)},
        )
        bundle = ContextBundle(
            messages=messages,
            history_kept=kept_history,
            tokens_estimate=used,
            blocks=blocks,
            dropped=dropped,
            compacted=compacted,
            digest=digest,
            messages_artifact_id=artifact.artifact_id,
        )
        return bundle

    # ------------------------------------------------------------------ system

    def _system_text(self, state: Any) -> str:
        from .prompt import build_system_prompt

        return build_system_prompt(self.config, state, self.project_instructions, self.role_instruction)

    # ------------------------------------------------------------------ frame

    def _frame_blocks(self, state: Any) -> list[ContextBlock]:
        blocks: list[ContextBlock] = []
        task = ContextBlock(
            block_id="task",
            source="task",
            content="## Task\n" + (getattr(state, "request", "") or "").strip(),
            required=True,
            priority=1000,
            score=100,
            reason="the user request is always present",
        )
        blocks.append(task)
        user_messages = getattr(state, "user_messages", []) or []
        if user_messages:
            blocks.append(
                ContextBlock(
                    block_id="user_inputs",
                    source="task",
                    content="## User answers / clarifications\n" + "\n".join(f"- {m}" for m in user_messages[-5:]),
                    required=True,
                    priority=990,
                    score=95,
                    reason="answers to blockers must never be dropped",
                )
            )
        plan_text = self._plan_text(state)
        blocks.append(
            ContextBlock(
                block_id="plan",
                source="plan",
                content=plan_text,
                required=True,
                priority=950,
                score=90,
                reason="the plan is runtime state; completion is checked against it",
            )
        )
        facts = self._memory_text(state)
        if facts.strip():
            blocks.append(
                ContextBlock(
                    block_id="facts",
                    source="facts",
                    content=facts,
                    priority=900,
                    score=70,
                    reason="verified facts must survive compaction",
                )
            )
        status = self._status_text(state)
        blocks.append(
            ContextBlock(
                block_id="status",
                source="status",
                content=status,
                required=True,
                priority=940,
                score=85,
                reason="current phase, budget and budget usage",
            )
        )
        diff = self._diff_block(state)
        if diff:
            blocks.append(diff)
        verification = self._verification_text(state)
        if verification:
            blocks.append(
                ContextBlock(
                    block_id="verification",
                    source="verification",
                    content=verification,
                    priority=880,
                    score=85,
                    reason="latest verification results determine what to fix next",
                )
            )
        failure = self._failure_block(state)
        if failure:
            blocks.append(failure)
        return blocks

    def _plan_text(self, state: Any) -> str:
        plan = getattr(state, "plan", None)
        if plan is None or not plan.is_effective():
            return "## Plan\n(no plan yet — investigate first, then call update_plan with concrete steps)"
        lines = [f"## Plan (v{plan.version}): {plan.objective}"]
        marks = {"completed": "[x]", "in_progress": "[>]", "blocked": "[!]", "skipped": "[-]", "failed": "[X]"}
        for step in plan.steps:
            mark = marks.get(step.status, "[ ]")
            lines.append(f"{mark} {step.step_id}: {step.description}")
            for ev in step.evidence[:3]:
                lines.append(f"      evidence: {ev}")
        if plan.verification_strategy:
            lines.append("verification: " + "; ".join(plan.verification_strategy))
        return "\n".join(lines)

    def _memory_text(self, state: Any) -> str:
        parts: list[str] = []
        facts = getattr(state, "facts", [])
        if facts:
            parts.append("## Facts")
            for fact in facts[-12:]:
                parts.append(f"- {fact.statement} ({fact.confidence}; {fact.source_reference or fact.source_type})")
        decisions = getattr(state, "decisions", [])
        if decisions:
            parts.append("## Decisions")
            for decision in decisions[-6:]:
                parts.append(f"- {decision.question} -> {decision.selected_option} ({decision.reason})")
        return "\n".join(parts)

    def _status_text(self, state: Any) -> str:
        return (
            "## State\n"
            f"phase: {state.phase}  step: {state.step_index}  model_calls: {state.model_calls}  "
            f"tool_calls: {state.tool_calls_total}  compactions: {state.compactions}\n"
            f"workspace: {state.workspace}"
        )

    def _diff_block(self, state: Any) -> ContextBlock | None:
        changed = sorted(set(getattr(state, "written_files", {})) | set(getattr(state, "deleted_files", [])))
        if not changed:
            return None
        lines = ["## Working tree changes (this session)"]
        for path in changed[:20]:
            meta = (state.written_files or {}).get(path) or {}
            if path in (state.deleted_files or []):
                lines.append(f"D {path}")
            else:
                lines.append(f"M {path} (before={str(meta.get('hash_before'))[:8]} after={str(meta.get('hash_after'))[:8]})")
        res = self.sandbox.run(["git", "diff", "--stat", "--no-color"], str(state.workspace), timeout=30)
        if res.exit_code == 0 and (res.stdout or "").strip():
            lines.append("```")
            lines.append((res.stdout or "").strip()[:3000])
            lines.append("```")
        return ContextBlock(
            block_id="diff",
            source="diff",
            content="\n".join(lines),
            priority=870,
            score=80,
            reason="the model must review its own change set before verification",
        )

    def _verification_text(self, state: Any) -> str:
        runs = getattr(state, "verification_runs", [])
        if not runs:
            return ""
        lines = ["## Verification"]
        for run in runs[-6:]:
            detail = ""
            if run.summary:
                detail = " " + json.dumps(run.summary, ensure_ascii=False)[:300]
            if run.findings:
                detail += " | " + "; ".join(run.findings[:2])
            lines.append(f"- {run.level} {run.type}: {run.status}{detail}")
        return "\n".join(lines)

    def _failure_block(self, state: Any) -> ContextBlock | None:
        err = getattr(state, "last_error", None)
        if not err or getattr(state, "consecutive_tool_failures", 0) == 0:
            return None
        return ContextBlock(
            block_id="failure",
            source="failure",
            content="## Last failure\n" + json.dumps(err, ensure_ascii=False)[:1500],
            priority=860,
            score=78,
            reason="the model must fix the current failure before anything else",
        )

    # ------------------------------------------------------------------ retrieval

    def _retrieval_blocks(self, state: Any, budget: int) -> list[ContextBlock]:
        candidates: dict[str, float] = {}
        reasons: dict[str, str] = {}

        def add(path: str, score: float, reason: str) -> None:
            if path not in candidates or candidates[path] < score:
                candidates[path] = score
                reasons[path] = reason

        workspace = Path(getattr(state, "workspace", "."))
        for path in (getattr(state, "read_hashes", {}) or {}):
            add(path, 1.0, "recently read")
        for path in (getattr(state, "written_files", {}) or {}):
            add(path, 3.0, "changed by this session")
        for path in (getattr(state, "deleted_files", []) or []):
            add(path, 2.0, "deleted by this session")
        task_text = (getattr(state, "request", "") or "").lower()
        plan = getattr(state, "plan", None)
        plan_text = plan.objective.lower() if plan else ""
        err = getattr(state, "last_error", None) or {}
        err_text = (json.dumps(err, ensure_ascii=False) or "").lower()

        # task-mentioned paths
        import re

        for token in re.findall(r"[A-Za-z0-9_./\\-]{3,}", getattr(state, "request", "") or ""):
            candidate = workspace / token
            if candidate.is_file():
                add(token.replace("\\", "/"), 4.0, "mentioned in the task")

        max_file_tokens = int(self.ctx_cfg.get("max_file_tokens", 12000))
        max_files = 4
        blocks: list[ContextBlock] = []
        for path, score in sorted(candidates.items(), key=lambda kv: -kv[1])[:max_files]:
            full = workspace / path
            if not full.is_file():
                continue
            try:
                body = full.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            digest = (getattr(state, "read_hashes", {}) or {}).get(path, "")
            if len(body) > max_file_tokens * 3:
                body = body[: max_file_tokens * 3] + f"\n... (file truncated at {max_file_tokens * 3} chars)"
            text = wrap_untrusted(path, body, digest)
            reason = reasons.get(path, "retrieval")
            if path.lower() in task_text or path.lower() in plan_text:
                score += 2
                reason += "; related to task/plan"
            if path.lower() in err_text:
                score += 1
                reason += "; referenced by the last error"
            blocks.append(
                ContextBlock(
                    block_id="file:" + path,
                    source="file",
                    content=text,
                    priority=600,
                    score=score * 10,
                    reason=reason,
                )
            )
        return blocks

    # ------------------------------------------------------------------ history

    def _fit_history(
        self,
        history: list[Message],
        used: int,
        soft: float,
        hard: float,
        budget: int,
    ) -> tuple[list[Message], str, bool, list[dict[str, Any]]]:
        keep = int(self.ctx_cfg.get("max_recent_steps", 8)) * 2
        hist_tokens = sum(estimate_tokens(m.content or "") for m in history)
        if used + hist_tokens <= soft:
            return list(history), "", False, []
        # compact: keep the most recent messages, digest the rest
        kept = history[-keep:]
        dropped_messages = history[:-keep]
        digest = self._history_digest(dropped_messages, len(history), used)
        dropped = [
            {"id": f"history[{i}]", "source": "history", "tokens": estimate_tokens(m.content or ""), "reason": "compacted"}
            for i, m in enumerate(dropped_messages)
        ]
        if used + sum(estimate_tokens(m.content or "") for m in kept) + estimate_tokens(digest) > hard:
            kept = kept[-max(2, keep // 2):]
        return kept, digest, bool(dropped_messages), dropped

    def _history_digest(self, dropped: list[Message], total: int, used: int) -> str:
        lines = [
            "## Compacted history",
            f"({len(dropped)} earlier messages of {total} were replaced by this digest; nothing "
            "was silently truncated — the harness keeps facts, plan, verification and artifact ids.)",
        ]
        for message in dropped[-6:]:
            body = (message.content or "").strip().replace("\n", " ")
            lines.append(f"- {message.role}: {body[:300]}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ render

    def _render_frame(self, blocks: list[ContextBlock], state: Any) -> str:
        parts = [b.content for b in blocks if b.source in ("task", "status")]
        plan = [b.content for b in blocks if b.source == "plan"]
        rest = [b for b in blocks if b.source not in ("task", "status", "plan")]
        out = ["# Working context", "", *parts, "", *plan, ""]
        for block in sorted(rest, key=lambda b: -b.priority):
            out.append(block.content)
            out.append("")
        out.append(
            "Decide the next action. If the task is complete, call propose_completion with claims "
            "that cite evidence; otherwise call exactly the tools you need next."
        )
        return "\n".join(out)
