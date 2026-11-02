"""DeepSeek / OpenAI-compatible Chat Completions provider.

Implements SSE streaming, tool-call delta accumulation, retries with backoff and
strict response validation. No third-party HTTP library: urllib is enough and
keeps the package installable without network access.

Tool-call accumulation rules (the part that bites people):
  * deltas carry an `index`; `id`/`name` usually arrive once, arguments stream in
  * arguments are concatenated per index, then parsed as JSON
  * a truncated stream (`finish_reason == "length"`) may leave invalid JSON; we
    surface `parse_error` instead of crashing
"""

from __future__ import annotations

import json
import socket
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from ..domain import ModelResponse, ToolCallRequest, TokenUsage, new_id
from ..errors import (
    MODEL_AUTH,
    MODEL_CONNECTION,
    MODEL_INVALID_RESPONSE,
    MODEL_RATE_LIMIT,
    MODEL_TIMEOUT,
    AgentError,
)
from .base import Capabilities, DeltaCallback, ModelRequest

_RETRIABLE_HTTP = {408, 409, 429, 500, 502, 503, 504}


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None,
        capabilities: Capabilities | None = None,
        max_retries: int = 3,
        timeout_seconds: float = 180.0,
        logger: Callable[[str], None] | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.capabilities = capabilities or Capabilities(
            name=f"openai_compatible:{model}",
            context_window=131072,
            max_output_tokens=8192,
        )
        self.max_retries = max(0, int(max_retries))
        self.timeout_seconds = timeout_seconds
        self.reasoning_effort = reasoning_effort
        self._log = logger or (lambda msg: None)

    # ------------------------------------------------------------------ public

    def complete(
        self, request: ModelRequest, on_text_delta: DeltaCallback | None = None
    ) -> ModelResponse:
        if not self.api_key:
            raise AgentError(
                MODEL_AUTH,
                "no API key: set the environment variable configured in [model].api_key_env "
                "(default DEEPSEEK_API_KEY)",
                source="model",
            )
        last_error: AgentError | None = None
        for attempt in range(self.max_retries + 1):
            try:
                if request.stream:
                    try:
                        return self._complete_stream(request, on_text_delta)
                    except AgentError as exc:
                        # some compatible servers reject stream_options; retry once without
                        if exc.code == MODEL_INVALID_RESPONSE and "stream_options" in exc.message:
                            return self._complete_stream(request, on_text_delta, include_usage=False)
                        raise
                return self._complete_blocking(request)
            except AgentError as exc:
                last_error = exc
                if not exc.retriable or attempt >= self.max_retries:
                    raise
                delay = min(2**attempt, 8)
                self._log(f"model attempt {attempt + 1} failed ({exc.code}); retry in {delay}s")
                time.sleep(delay)
            except (socket.timeout, TimeoutError) as exc:
                last_error = AgentError(MODEL_TIMEOUT, str(exc), retriable=True, source="model")
                if attempt >= self.max_retries:
                    raise last_error from exc
                time.sleep(min(2**attempt, 8))
        raise last_error or AgentError(MODEL_INVALID_RESPONSE, "unknown model failure", source="model")

    # ------------------------------------------------------------------ http

    def _endpoint(self) -> str:
        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    def _payload(self, request: ModelRequest, stream: bool, include_usage: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_api() for m in request.messages],
            "stream": stream,
        }
        if request.tools and self.capabilities.supports_tools:
            payload["tools"] = request.tools
            payload["tool_choice"] = "auto"
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        if request.max_output_tokens:
            payload["max_tokens"] = request.max_output_tokens
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
        if stream and include_usage:
            payload["stream_options"] = {"include_usage": True}
        return payload

    def _request(self, request: ModelRequest, stream: bool, include_usage: bool = True):
        data = json.dumps(self._payload(request, stream, include_usage)).encode("utf-8")
        req = urllib.request.Request(
            self._endpoint(),
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
                "Accept": "text/event-stream" if stream else "application/json",
            },
        )
        try:
            return urllib.request.urlopen(req, timeout=request.timeout_seconds or self.timeout_seconds)
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:2000]
            except Exception:
                pass
            raise _http_error(exc.code, body) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise AgentError(MODEL_TIMEOUT, str(exc), retriable=True, source="model") from exc
        except urllib.error.URLError as exc:
            raise AgentError(
                MODEL_CONNECTION, f"cannot reach model endpoint: {exc.reason}", retriable=True, source="model"
            ) from exc

    # ------------------------------------------------------------------ modes

    def _complete_blocking(self, request: ModelRequest) -> ModelResponse:
        with self._request(request, stream=False) as resp:
            raw = _decode_provider_bytes(resp.read())
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AgentError(MODEL_INVALID_RESPONSE, f"invalid JSON from provider: {raw[:500]}", source="model") from exc
        return self._parse_message(data, raw)

    def _complete_stream(
        self,
        request: ModelRequest,
        on_text_delta: DeltaCallback | None,
        include_usage: bool = True,
    ) -> ModelResponse:
        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        finish_reason = ""
        usage = TokenUsage()
        response_id = ""
        raw_lines: list[str] = []

        with self._request(request, stream=True, include_usage=include_usage) as resp:
            for raw_line in resp:
                line = _decode_provider_bytes(raw_line).strip()
                if not line:
                    continue
                raw_lines.append(line)
                if not line.startswith("data:"):
                    continue
                data_str = line[5:].strip()
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                if chunk.get("id"):
                    response_id = chunk["id"]
                if chunk.get("usage"):
                    usage = _parse_usage(chunk["usage"])
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    text_parts.append(content)
                    if on_text_delta:
                        on_text_delta(content)
                for idx, tc in enumerate(delta.get("tool_calls") or []):
                    index = tc.get("index", idx)
                    slot = calls.setdefault(index, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = tc["id"]
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = fn["name"]
                    if fn.get("arguments"):
                        slot["arguments"] += fn["arguments"]
                if choice.get("finish_reason"):
                    finish_reason = choice["finish_reason"]

        if not response_id:
            response_id = new_id("resp")
        text = "".join(text_parts)
        tool_calls = _finalize_calls(calls)
        if not text and not tool_calls and finish_reason not in ("stop", "length", "tool_calls"):
            raise AgentError(
                MODEL_INVALID_RESPONSE,
                f"empty response with finish_reason={finish_reason!r}: {''.join(raw_lines)[:500]}",
                source="model",
            )
        return ModelResponse(
            response_id=response_id,
            text=text,
            tool_calls=tool_calls,
            finish_reason=finish_reason or ("tool_calls" if tool_calls else "stop"),
            usage=usage,
            model=self.model,
        )

    def _parse_message(self, data: dict[str, Any], raw: str) -> ModelResponse:
        choices = data.get("choices") or []
        if not choices:
            raise AgentError(MODEL_INVALID_RESPONSE, f"no choices: {raw[:500]}", source="model")
        choice = choices[0]
        msg = choice.get("message") or {}
        calls: dict[int, dict[str, Any]] = {}
        for idx, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            calls[idx] = {
                "id": tc.get("id") or "",
                "name": fn.get("name") or "",
                "arguments": fn.get("arguments") or "",
            }
        return ModelResponse(
            response_id=data.get("id") or new_id("resp"),
            text=msg.get("content") or "",
            tool_calls=_finalize_calls(calls),
            finish_reason=choice.get("finish_reason") or "stop",
            usage=_parse_usage(data.get("usage") or {}),
            model=data.get("model") or self.model,
        )


# --------------------------------------------------------------------------- helpers


def _decode_provider_bytes(raw: bytes) -> str:
    """Decode provider payloads strictly; replacement characters hide wire corruption."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentError(
            MODEL_INVALID_RESPONSE,
            "provider response is not valid UTF-8; check the configured model endpoint and response encoding",
            source="model",
        ) from exc


def _finalize_calls(calls: dict[int, dict[str, Any]]) -> list[ToolCallRequest]:
    out: list[ToolCallRequest] = []
    for index in sorted(calls):
        slot = calls[index]
        name = slot.get("name") or ""
        raw_args = slot.get("arguments") or ""
        parsed = None
        parse_error = None
        if name:
            if not raw_args.strip():
                parsed = {}
            else:
                try:
                    parsed = json.loads(raw_args)
                except json.JSONDecodeError as exc:
                    parsed = None
                    parse_error = str(exc)
        out.append(
            ToolCallRequest(
                id=slot.get("id") or new_id("call"),
                name=name,
                arguments=raw_args,
                parsed=parsed if isinstance(parsed, dict) else None,
                parse_error=parse_error,
            )
        )
    return out


def _parse_usage(usage: dict[str, Any]) -> TokenUsage:
    return TokenUsage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
    )


def _http_error(status: int, body: str) -> AgentError:
    message = f"HTTP {status}: {body[:800]}"
    if status == 401 or status == 403:
        return AgentError(MODEL_AUTH, message, source="model")
    if status == 429:
        return AgentError(MODEL_RATE_LIMIT, message, retriable=True, source="model")
    if status in _RETRIABLE_HTTP:
        return AgentError(MODEL_CONNECTION, message, retriable=True, source="model")
    return AgentError(MODEL_INVALID_RESPONSE, message, source="model")
