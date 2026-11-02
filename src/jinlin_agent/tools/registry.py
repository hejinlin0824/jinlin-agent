"""Tool registry: one place that knows every capability the model has."""

from __future__ import annotations

from typing import Any

from ..domain import SideEffect
from .base import ToolContext, ToolResult, ToolSpec


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[ToolSpec]:
        return [self._tools[n] for n in self.names()]

    def definitions(self, *, read_only: bool = False) -> list[dict[str, Any]]:
        """OpenAI function-tool definitions, scoped to the current task mode."""
        out = []
        specs = self.all()
        if read_only:
            # Expose only observational tools. Session-state workflow tools
            # (plan/facts/completion) create needless turns for simple analysis;
            # write and execute tools must not be advertised at all.
            specs = [
                spec for spec in specs
                if spec.side_effect == SideEffect.READ.value or spec.name == "request_user_input"
            ]
        for spec in specs:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": spec.name,
                        "description": spec.description,
                        "parameters": spec.schema,
                    },
                }
            )
        return out


def build_default_registry() -> ToolRegistry:
    from . import fs, git, patch, project, shell, task

    registry = ToolRegistry()
    for module in (fs, project, patch, shell, git, task):
        registry = module.register(registry)
    return registry
