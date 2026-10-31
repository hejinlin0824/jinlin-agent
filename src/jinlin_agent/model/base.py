"""Provider protocol and shared request/response types.

Providers are synchronous: the harness loop is single threaded, background
processes use their own threads, and streaming deltas are delivered through an
optional callback. This keeps the critical path easy to reason about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from ..domain import Message, ModelResponse


@dataclass
class Capabilities:
    name: str = "unknown"
    context_window: int = 131072
    max_output_tokens: int = 8192
    supports_tools: bool = True
    supports_parallel_tool_calls: bool = True
    supports_streaming: bool = True
    tool_call_format: str = "openai"
    tokenizer: str = "heuristic"

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


@dataclass
class ModelRequest:
    request_id: str
    messages: list[Message]
    tools: list[dict[str, Any]] = field(default_factory=list)
    temperature: float = 0.0
    max_output_tokens: int | None = None
    stream: bool = True
    timeout_seconds: float = 180.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "messages": [m.to_dict() for m in self.messages],
            "tools": self.tools,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "stream": self.stream,
        }


DeltaCallback = Callable[[str], None]


class Provider(Protocol):
    name: str
    capabilities: Capabilities

    def complete(self, request: ModelRequest, on_text_delta: DeltaCallback | None = None) -> ModelResponse:
        ...
