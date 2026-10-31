"""Tool contract and execution context.

A tool is a pure-ish function: it validates nothing (the dispatcher does), does
one thing, and returns a ToolResult. Every observable side effect must be
reported back in the result so the dispatcher can write it into the event log.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..domain import SideEffect
from ..errors import AgentError


@dataclass
class ToolResult:
    ok: bool = True
    output: str = ""  # model-facing text
    artifact_content: str | None = None  # full payload, stored when large
    artifact_kind: str = "tool_output"
    error: AgentError | None = None
    exit_code: int | None = None
    timed_out: bool = False
    written_files: list[dict[str, Any]] = field(default_factory=list)
    deleted_files: list[str] = field(default_factory=list)
    read_hashes: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: dict[str, Any]
    handler: Callable[["ToolContext", dict[str, Any]], ToolResult]
    side_effect: str = SideEffect.READ.value
    timeout_seconds: int = 120
    idempotent: bool = True
    max_output_chars: int = 20_000
    group: str = "general"


@dataclass
class ToolContext:
    workspace: Path
    config: Any
    sandbox: Any
    artifacts: Any
    state: Any = None
    session_dir: Path | None = None
    processes: dict[str, Any] = field(default_factory=dict)
    log: Callable[[str], None] = lambda msg: None

    # ------------------------------------------------------------------ paths

    def resolve(self, path: str, must_exist: bool = False) -> Path:
        """Resolve a repo-relative (or absolute-inside-workspace) path."""
        raw = str(path or ".")
        candidate = Path(raw)
        if candidate.is_absolute():
            resolved = Path(os.path.abspath(candidate))
        else:
            resolved = Path(os.path.abspath(self.workspace / candidate))
        return resolved

    def relative(self, path: Path) -> str:
        try:
            return Path(path).resolve().relative_to(Path(self.workspace).resolve()).as_posix()
        except ValueError:
            return str(path)
