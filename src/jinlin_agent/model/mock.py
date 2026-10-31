"""Scripted provider: deterministic model behaviour for tests and conformance.

A script is a list of steps. Each step is one of:
  {"text": "..."}                                  plain assistant text
  {"tool_calls": [{"name": ..., "arguments": {...}}]}   emit tool calls
  {"text": "...", "tool_calls": [...]}             both
  {"raise": {"code": "MODEL_TIMEOUT", "message": ...}}  fail the request
  {"raw_arguments": "{not json"}                   malformed tool arguments
  a callable(messages) -> step dict               dynamic scripts

Every request is recorded in `self.requests` so tests can assert on the exact
context the harness built (that is how T3 is verified).
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..domain import ModelResponse, ToolCallRequest, TokenUsage, new_id
from ..errors import MODEL_INVALID_RESPONSE, AgentError
from .base import Capabilities, DeltaCallback, ModelRequest

Step = dict[str, Any] | Callable[[list[Any]], dict[str, Any]]


class MockProvider:
    name = "mock"

    def __init__(self, script: list[Step], name: str = "mock") -> None:
        self.script = list(script)
        self.capabilities = Capabilities(
            name=name,
            context_window=32768,
            max_output_tokens=2048,
            supports_streaming=True,
        )
        self.requests: list[ModelRequest] = []
        self._index = 0

    # ------------------------------------------------------------------ api

    def complete(
        self, request: ModelRequest, on_text_delta: DeltaCallback | None = None
    ) -> ModelResponse:
        self.requests.append(request)
        if self._index >= len(self.script):
            raise AgentError(
                MODEL_INVALID_RESPONSE,
                f"mock script exhausted after {len(self.script)} steps",
                source="mock",
            )
        step = self.script[self._index]
        self._index += 1
        if callable(step):
            step = step([m.to_dict() for m in request.messages])
        if not isinstance(step, dict):
            raise AgentError(MODEL_INVALID_RESPONSE, "mock step must be a dict", source="mock")

        if "raise" in step:
            info = step["raise"]
            raise AgentError(
                info.get("code", MODEL_INVALID_RESPONSE),
                info.get("message", "scripted failure"),
                retriable=bool(info.get("retriable", False)),
                source="mock",
            )

        text = step.get("text", "") or ""
        if text and on_text_delta:
            on_text_delta(text)
        calls: list[ToolCallRequest] = []
        for i, spec in enumerate(step.get("tool_calls") or []):
            name = spec.get("name", "")
            if "raw_arguments" in spec:
                raw = str(spec["raw_arguments"])
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = None
            else:
                parsed = spec.get("arguments", {})
                raw = json.dumps(parsed, ensure_ascii=False)
            calls.append(
                ToolCallRequest(
                    id=spec.get("id") or f"call_mock_{self._index}_{i}",
                    name=name,
                    arguments=raw,
                    parsed=parsed if isinstance(parsed, dict) else None,
                    parse_error=None if isinstance(parsed, dict) else "scripted malformed arguments",
                )
            )
        finish = step.get("finish_reason") or ("tool_calls" if calls else "stop")
        usage = TokenUsage(
            prompt_tokens=step.get("prompt_tokens", 100),
            completion_tokens=step.get("completion_tokens", 20),
            total_tokens=step.get("prompt_tokens", 100) + step.get("completion_tokens", 20),
        )
        return ModelResponse(
            response_id=new_id("resp_mock"),
            text=text,
            tool_calls=calls,
            finish_reason=finish,
            usage=usage,
            model=self.name,
        )
