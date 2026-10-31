"""Read-only git tools. No commit/push in V1 (those need approval and are out of scope)."""

from __future__ import annotations

from typing import Any

from ..errors import TOOL_EXECUTION_FAILED, AgentError
from .base import ToolContext, ToolResult, ToolSpec
from .registry import ToolRegistry


def _git(ctx: ToolContext, args: list[str], timeout: int = 60):
    return ctx.sandbox.run(["git", *args], str(ctx.workspace), timeout=timeout)


def _not_repo(result) -> bool:
    return result.exit_code not in (0, 1) and "not a git repository" in (result.stderr or "").lower()


def get_git_status(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    res = _git(ctx, ["status", "--porcelain=v1", "-b"])
    if _not_repo(res):
        return ToolResult(output="<git_status>not a git repository</git_status>")
    if res.exit_code != 0:
        return ToolResult(ok=False, error=AgentError(TOOL_EXECUTION_FAILED, res.stderr.strip()[:1000], source="git"))
    out = res.stdout.strip() or "(clean)"
    return ToolResult(output=f'<git_status>\n{out}\n</git_status>')


def get_git_diff(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    cmd = ["diff", "--no-color"]
    if args.get("staged"):
        cmd.insert(1, "--cached")
    if args.get("stat_only"):
        cmd.append("--stat")
    path = args.get("path")
    if path:
        cmd += ["--", str(path)]
    res = _git(ctx, cmd)
    if _not_repo(res):
        return ToolResult(output="<git_diff>not a git repository</git_diff>")
    if res.exit_code != 0:
        return ToolResult(ok=False, error=AgentError(TOOL_EXECUTION_FAILED, res.stderr.strip()[:1000], source="git"))
    diff = res.stdout or ""
    if not diff.strip():
        return ToolResult(output="<git_diff>(no unstaged changes)</git_diff>")
    return ToolResult(
        output=f"<git_diff>\n{diff}\n</git_diff>",
        artifact_content=diff,
        extra={"stat": bool(args.get("stat_only"))},
    )


def get_git_log(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    n = int(args.get("max_entries", 10))
    res = _git(ctx, ["log", f"-n{n}", "--oneline", "--decorate"])
    if _not_repo(res) or res.exit_code != 0:
        return ToolResult(output="<git_log>(no history)</git_log>")
    return ToolResult(output=f"<git_log>\n{res.stdout.strip()}\n</git_log>")


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(
        ToolSpec(
            name="get_git_status",
            description="Show current branch and working tree status (git status --porcelain).",
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=get_git_status,
            group="git",
            idempotent=True,
        )
    )
    registry.register(
        ToolSpec(
            name="get_git_diff",
            description="Show the working tree diff (read-only). Use stat_only for a summary of changed files.",
            schema={
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean"},
                    "stat_only": {"type": "boolean"},
                    "path": {"type": "string"},
                },
                "additionalProperties": False,
            },
            handler=get_git_diff,
            group="git",
            idempotent=True,
            max_output_chars=30_000,
        )
    )
    registry.register(
        ToolSpec(
            name="get_git_log",
            description="Show recent commits (oneline).",
            schema={
                "type": "object",
                "properties": {"max_entries": {"type": "integer", "minimum": 1, "maximum": 100}},
                "additionalProperties": False,
            },
            handler=get_git_log,
            group="git",
            idempotent=True,
        )
    )
    return registry
