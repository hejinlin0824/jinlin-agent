"""Tool registry: one place that knows every capability the model has."""

from __future__ import annotations

from typing import Any

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

    def definitions(self) -> list[dict[str, Any]]:
        """OpenAI function-tool definitions."""
        out = []
        for spec in self.all():
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

    def view(self, allowed) -> "ToolRegistry":
        """A subset view for role whitelists (v2 §6.2). Unknown names are
        ignored so a RoleSpec may list tools from a newer registry; the
        dispatcher simply cannot reach anything outside the whitelist."""
        reg = ToolRegistry()
        for name in allowed:
            spec = self._tools.get(name)
            if spec is not None:
                reg.register(spec)
        return reg


def build_default_registry() -> ToolRegistry:
    from . import fs, git, patch, project, shell, task

    registry = ToolRegistry()
    for module in (fs, project, patch, shell, git, task):
        registry = module.register(registry)
    return registry
