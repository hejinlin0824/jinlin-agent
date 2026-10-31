"""Provider factory."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import CONFIG_INVALID, AgentError
from .base import Capabilities, ModelRequest, Provider
from .mock import MockProvider
from .openai_compat import OpenAICompatibleProvider

__all__ = ["Provider", "ModelRequest", "Capabilities", "create_provider", "MockProvider", "OpenAICompatibleProvider"]


def create_provider(config: Any, mock_script: Path | str | None = None) -> Provider:
    if mock_script is not None:
        return MockProvider(load_script(mock_script))
    model = config.model.model
    if model.startswith("mock"):
        raise AgentError(
            CONFIG_INVALID,
            "model is 'mock': pass --mock-script <file.json> (or use the eval suite)",
            source="config",
        )
    return OpenAICompatibleProvider(
        base_url=config.model.base_url,
        model=model,
        api_key=config.api_key(),
        capabilities=Capabilities(
            name=f"openai_compatible:{model}",
            context_window=int(config.model.context_window),
            max_output_tokens=int(config.model.max_output_tokens),
        ),
        max_retries=int(config.model.max_retries),
        timeout_seconds=float(config.model.timeout_seconds),
        reasoning_effort=getattr(config.model, "reasoning_effort", None) or None,
    )


def load_script(path: Path | str) -> list[dict[str, Any]]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AgentError(CONFIG_INVALID, f"cannot load mock script {p}: {exc}", source="config") from exc
    if isinstance(data, dict):
        data = data.get("steps", [])
    if not isinstance(data, list):
        raise AgentError(CONFIG_INVALID, "mock script must be a JSON list of steps", source="config")
    return data
