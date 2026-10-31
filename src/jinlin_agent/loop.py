"""The agent loop and its harness wiring.

Termination is a state machine decision, not a model opinion:
  propose_completion -> deterministic verification ladder -> completion gate.
If the gate rejects, the loop continues with the rejection as an observation.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from .config import Config
from .context import ContextBuilder
from .domain import (
    Claim,
    Message,
    Phase,
    Status,
    ToolCallRequest,
    new_id,
)
from .errors import (
    BUDGET_EXCEEDED,
    COMPLETION_REJECTED,
    MODEL_CONTEXT_OVERFLOW,
    AgentError,
)
from .prompt import load_project_instructions
from .report import write_report
from .runtime import ToolDispatcher
from .session import Session
from .state import apply_event
from .tokens import estimate_messages_tokens
from .tools.base import ToolContext
from .tools.registry import build_default_registry
from .policy import PolicyEngine
from .verify import CompletionGate, Verifier, detect_project


class Harness:
    def __init__(
        self,
        session: Session,
        config: Config,
        provider: Any,
        approval_handler: Callable[[Any], tuple[str, str]] | None = None,
        auto_approve: bool = False,
        read_only: bool = False,
        stream_callback: Callable[[str], None] | None = None,
        log: Callable[[str], None] = lambda msg: None,
    ) -> None:
        self.session = session
        self.config = config
        self.provider = provider
        self.state = session.state
        self.log = log
        self.stream_callback = stream_callback
        self.harness_verifier = Verifier(
            session.workspace,
            config,
            session.sandbox,
            session.artifacts,
            emit=lambda t, p: session.emit(t, p),
            state=self.state,
        )
        self.project = detect_project(session.workspace, config)
        self.project_instructions = load_project_instructions(
            session.workspace, config.project.get("instruction_files", [])
        )
        self.registry = build_default_registry()
        self.policy = PolicyEngine(
            config,
            session.workspace,
            approval_handler=approval_handler,
            auto_approve=auto_approve,
            read_only=read_only,
        )
        self.ctx = ToolContext(
            workspace=session.workspace,
            config=config,
            sandbox=session.sandbox,
            artifacts=session.artifacts,
            state=self.state,
            session_dir=session.dir,
            log=log,
        )
        self.dispatcher = ToolDispatcher(
            registry=self.registry,
            policy=self.policy,
            artifacts=session.artifacts,
            emit=self._emit_tool,
            ctx=self.ctx,
            config=config,
        )
        self.context_builder = ContextBuilder(
            config=config,
            capabilities=provider.capabilities,
            artifacts=session.artifacts,
            sandbox=session.sandbox,
            project_instructions=self.project_instructions,
        )
        self.history: list[Message] = self._restore_history()
        self.invalid_decisions = 0
        self.turns = 0
        self._started = time.monotonic()

    # ------------------------------------------------------------------ helpers

    def _emit_tool(self, type: str, payload: dict[str, Any], turn_id: str = "", step_id: str = "") -> dict[str, Any]:
        event = self.session.log.append(type, payload, turn_id, step_id)
        apply_event(self.state, event)
        return event

    def _tool_event(self, type: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self._emit_tool(type, payload, self.turn_id, self.step_id)

    def _restore_history(self) -> list[Message]:
        history: list[Message] = []
        observations = {o.get("tool_call_id"): o for o in (self.state.recent_observations or [])}
        for msg in self.state.recent_assistant or []:
            calls = [ToolCallRequest.from_dict(c) for c in (msg.get("tool_calls") or [])]
            text = msg.get("text", "")
            if calls:
                history.append(Message(role="assistant", content=text, tool_calls=calls))
                for call in calls:
                    obs = observations.get(call.id)
                    if obs:
                        prefix = "" if obs.get("ok", True) else "ERROR: "
                        history.append(
                            Message(
                                role="tool",
                                content=prefix + str(obs.get("preview", ""))[:6000],
                                tool_call_id=call.id,
                                name=call.name,
                            )
                        )
            elif text:
                history.append(Message(role="assistant", content=text))
        return history

    def _advance_phase(self) -> None:
        state = self.state
        plan = state.plan
        phase = state.phase
        new_phase = phase
        if phase in (Phase.CREATED.value, Phase.INITIALIZING.value):
            new_phase = Phase.INVESTIGATING.value
        elif phase == Phase.INVESTIGATING.value:
            new_phase = Phase.PLANNING.value if plan.is_effective() else Phase.INVESTIGATING.value
        elif phase in (Phase.PLANNING.value, Phase.IMPLEMENTING.value, Phase.REPAIRING.value, Phase.VERIFYING.value):
            last_run = state.verification_runs[-1] if state.verification_runs else None
            if last_run is not None and last_run.status == "failed":
                new_phase = Phase.REPAIRING.value
            elif plan.is_effective() and any(s.status in ("in_progress", "completed") for s in plan.steps):
                new_phase = Phase.VERIFYING.value if last_run is not None else Phase.IMPLEMENTING.value
            else:
                new_phase = Phase.PLANNING.value
        if new_phase != phase:
            self.session.emit("phase.changed", {"from": phase, "to": new_phase})

    def _check_budget(self) -> AgentError | None:
        cfg = self.config.runtime
        if self.state.step_index >= int(cfg.get("max_steps_per_session", 200)):
            return AgentError(BUDGET_EXCEEDED, "max_steps_per_session reached", source="runtime")
        if self.state.model_calls >= int(cfg.get("max_model_calls", 120)):
            return AgentError(BUDGET_EXCEEDED, "max_model_calls reached", source="runtime")
        if self.state.tokens_in + self.state.tokens_out > int(cfg.get("max_total_tokens", 4_000_000)):
            return AgentError(BUDGET_EXCEEDED, "max_total_tokens reached", source="runtime")
        if time.monotonic() - self._started > float(cfg.get("wall_clock_seconds", 14400)):
            return AgentError(BUDGET_EXCEEDED, "wall_clock_seconds reached", source="runtime")
        return None

    def _checkpoint_if_due(self) -> None:
        every = int(self.config.runtime.get("checkpoint_every_steps", 5))
        if every > 0 and self.state.step_index > 0 and self.state.step_index % every == 0:
            self.session.emit(
                "session.checkpoint",
                {
                    "seq": self.state.last_seq,
                    "state_hash": self.state.state_hash(),
                    "step_index": self.state.step_index,
                    "status": self.state.status,
                },
            )

    # ------------------------------------------------------------------ run

    def run(self) -> str:
        self.turn_id = new_id("turn")
        self.step_id = ""
        if self.state.is_terminal():
            return self.state.status
        try:
            while not self.state.is_terminal():
                budget_error = self._check_budget()
                if budget_error is not None:
                    self._fail(budget_error)
                    break
                result = self._step()
                if result == "terminal":
                    break
        except KeyboardInterrupt:
            self.session.emit("session.cancelled", {"reason": "interrupted by user"})
            self.session.close()
            return Status.CANCELLED.value
        return self.state.status

    def _step(self) -> str:
        state = self.state
        self.step_id = f"step_{state.step_index + 1:04d}"
        self.session.emit("step.started", {"step_index": state.step_index + 1, "phase": state.phase}, self.turn_id, self.step_id)
        self._advance_phase()

        bundle = self.context_builder.build(state, self.history, state.request)
        self.history = list(bundle.history_kept)
        self._tool_event("context.built", bundle.to_event_payload())
        if bundle.compacted:
            self._tool_event(
                "context.compacted",
                {
                    "from_messages": len(self.history),
                    "blocks": [b.block_id for b in bundle.blocks],
                    "dropped": bundle.dropped[:20],
                    "tokens_estimate": bundle.tokens_estimate,
                },
            )

        request = self._build_request(bundle.messages)
        self._tool_event("model.requested", {"request_id": request.request_id, "model": self.config.model.model})
        try:
            response = self.provider.complete(request, on_text_delta=self.stream_callback)
        except AgentError as exc:
            self._tool_event("model.attempt", {"error_code": exc.code, "error_message": exc.message, "retriable": exc.retriable})
            if exc.code == MODEL_CONTEXT_OVERFLOW:
                self._tool_event("context.compacted", {"reason": "model reported context overflow", "dropped": ["history"]})
                return "continue"
            self._fail(exc)
            return "terminal"
        self._tool_event(
            "model.attempt",
            {
                "response_id": response.response_id,
                "finish_reason": response.finish_reason,
                "usage": response.usage.to_dict(),
            },
        )
        raw_artifact = self.session.artifacts.write_json(
            "model_response",
            response.to_dict(),
            meta={"step": state.step_index},
        )
        response.raw_artifact_id = raw_artifact.artifact_id
        self._tool_event(
            "model.responded",
            {
                "response_id": response.response_id,
                "text": response.text[:20_000],
                "tool_calls": [tc.to_dict() for tc in response.tool_calls],
                "finish_reason": response.finish_reason,
                "artifact_id": raw_artifact.artifact_id,
            },
        )
        self.history.append(
            Message(role="assistant", content=response.text, tool_calls=response.tool_calls)
        )
        if response.tool_calls:
            return self._handle_tool_calls(response.tool_calls)
        return self._handle_text_only(response)

    def _build_request(self, messages: list[Message]) -> Any:
        from .model.base import ModelRequest

        return ModelRequest(
            request_id=new_id("req"),
            messages=messages,
            tools=self.registry.definitions(),
            temperature=float(self.config.model.temperature),
            max_output_tokens=int(self.config.model.max_output_tokens),
            stream=True,
            timeout_seconds=float(self.config.model.timeout_seconds),
        )

    # ------------------------------------------------------------------ branches

    def _handle_tool_calls(self, calls: list[ToolCallRequest]) -> str:
        completion: dict[str, Any] | None = None
        user_input: dict[str, Any] | None = None
        for call in calls:
            dispatch = self.dispatcher.execute(call, self.turn_id, self.step_id)
            result = dispatch.result
            if result.ok:
                body = result.output
            else:
                error = result.error or AgentError("TOOL_EXECUTION_FAILED", "failed", source=call.name)
                body = f"ERROR {error.code}: {error.message}"
                if result.output and result.output != error.message:
                    body += "\n" + result.output
            self.history.append(Message(role="tool", content=body, tool_call_id=call.id, name=call.name))
            if dispatch.completion is not None:
                completion = dispatch.completion
            if dispatch.user_input is not None:
                user_input = dispatch.user_input

        if completion is not None:
            return self._handle_completion(completion)
        if user_input is not None:
            self._tool_event("session.waiting_user", {"question": user_input})
            return "terminal"
        self._check_loop_guards()
        self._checkpoint_if_due()
        return "continue"

    def _handle_completion(self, completion: dict[str, Any]) -> str:
        state = self.state
        state.claims = [Claim.from_dict(c) for c in completion.get("claims", [])]
        # the harness verifies; the model's own claims are only inputs
        self._run_harness_verification(state.claims)
        gate = CompletionGate(
            workspace=self.session.workspace,
            config=self.config,
            state=state,
            sandbox=self.session.sandbox,
            verifier=self.harness_verifier,
        ).check(state.claims, completion.get("summary", ""))
        if gate.passed:
            self.state.phase = Phase.COMPLETED.value
            report_artifact = write_report(
                self.session.dir,
                self.session.artifacts,
                state,
                gate,
                self.config,
                provider_name=getattr(self.provider, "name", ""),
            )
            self.session.emit(
                "session.completed",
                {
                    "result": gate.result.value,
                    "summary": completion.get("summary", ""),
                    "report_artifact_id": report_artifact,
                    "warnings": gate.warnings,
                    "evidence": gate.evidence,
                },
            )
            self.log(f"[done] {gate.result.value} — report: {self.session.dir / 'report.md'}")
            return "terminal"
        self.session.emit(
            "completion.rejected",
            {"reasons": gate.reasons, "warnings": gate.warnings, "claims": [c.to_dict() for c in state.claims]},
        )
        self.log("[gate] completion rejected: " + "; ".join(gate.reasons[:4]))
        self.history.append(
            Message(
                role="user",
                content=(
                    "The harness completion gate REJECTED your completion proposal. Reasons:\n"
                    + "\n".join(f"- {r}" for r in gate.reasons)
                    + "\n\nFix these and propose completion again. Do not claim success without the required "
                    "verification evidence."
                ),
            )
        )
        self.invalid_decisions = 0
        return "continue"

    def _run_harness_verification(self, claims: list[Any] | None = None) -> None:
        state = self.state
        changed = bool(state.written_files or state.deleted_files)
        claims = claims or []
        mentions_tests = any(
            "test" in (getattr(c, "statement", "") or "").lower() or "pass" in (getattr(c, "statement", "") or "").lower()
            for c in claims
        )
        if not changed and not mentions_tests:
            return
        levels = list(self.config.verification.get("levels", ["L0", "L1", "L2", "L3", "L5"]))
        if changed and not self.config.verification.get("require_for_code_changes", True):
            return
        allowed = {"L0", "L1", "L2", "L3", "L5"}
        levels = [lvl for lvl in levels if lvl in allowed]
        # always run the deterministic core for code changes; tests whenever claims mention them
        core = [lvl for lvl in ("L0", "L1") if lvl not in levels]
        levels = core + levels
        if not self.harness_verifier.project.test_commands:
            levels = [lvl for lvl in levels if lvl not in ("L3", "L5")]
        if not changed:
            levels = [lvl for lvl in levels if lvl in ("L3", "L5")]
        self.harness_verifier.run_ladder(levels)

    def _handle_text_only(self, response: Any) -> str:
        self.invalid_decisions += 1
        if self.invalid_decisions >= 3:
            self._tool_event(
                "session.waiting_user",
                {
                    "question": (
                        "The model stopped making tool calls three times in a row. Recent output: "
                        + (response.text or "")[:500]
                    )
                },
            )
            return "terminal"
        self.history.append(
            Message(
                role="user",
                content=(
                    "You replied without calling a tool and without proposing completion. The task is not "
                    "finished by explaining. Continue: call the tools you need, or call propose_completion "
                    "with evidence if everything is verified."
                ),
            )
        )
        return "continue"

    # ------------------------------------------------------------------ guards

    def _check_loop_guards(self) -> None:
        state = self.state
        max_failures = int(self.config.runtime.get("max_consecutive_tool_failures", 5))
        max_sig = int(self.config.runtime.get("max_tool_signature_repeats", 3))
        reason = ""
        kind = ""
        if state.consecutive_tool_failures >= max_failures:
            kind, reason = "consecutive_failures", f"{state.consecutive_tool_failures} consecutive tool failures"
        elif state.signature_streak >= max_sig:
            kind, reason = "same_tool_signature", f"same tool call repeated {state.signature_streak}x with the same result"
        if not kind:
            return
        self.session.emit("loop.detected", {"kind": kind, "reason": reason, "signature": state.last_signature})
        self.history.append(
            Message(
                role="user",
                content=(
                    f"LOOP DETECTED: {reason}. Stop repeating. Re-read the relevant file or error, form a "
                    "different hypothesis, and change your approach."
                ),
            )
        )
        if state.loop_events >= 3:
            self._fail(AgentError("LOOP_DETECTED", reason, source="runtime"))
            return
        # reset counters so the next two rounds are fresh
        state.consecutive_tool_failures = 0
        state.signature_streak = 0

    # ------------------------------------------------------------------ terminal

    def _fail(self, error: AgentError) -> None:
        self.session.emit("session.failed", {"error": error.to_dict()})
        if self.state.report_artifact_id is None:
            gate = None
            try:
                report_artifact = write_report(
                    self.session.dir,
                    self.session.artifacts,
                    self.state,
                    gate,
                    self.config,
                    provider_name=getattr(self.provider, "name", ""),
                )
                self.session.emit("report.written", {"report_artifact_id": report_artifact, "reason": "session failed"})
            except Exception:
                pass
        self.log(f"[failed] {error.code}: {error.message}")
