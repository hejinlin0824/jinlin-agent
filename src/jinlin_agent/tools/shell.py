"""Shell tools: run_command / read_process_output / terminate_process.

Commands are argv arrays (never a shell string), executed through the sandbox,
with a resolved interpreter so Windows `.cmd` shims do not silently fall back to
`cmd /c`.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from ..domain import SideEffect, new_id
from ..errors import COMMAND_DENIED, TOOL_TIMEOUT, AgentError
from .base import ToolContext, ToolResult, ToolSpec
from .registry import ToolRegistry

_MAX_JOBS = 8

_PY_MODULES = {"pytest", "ruff", "mypy", "black", "flake8", "pip", "uvicorn", "gunicorn", "pydantic"}


def _venv_python(root: Path) -> Path | None:
    for candidate in (
        root / ".venv" / "Scripts" / "python.exe",
        root / "venv" / "Scripts" / "python.exe",
        root / ".venv" / "bin" / "python",
        root / "venv" / "bin" / "python",
    ):
        if candidate.exists():
            return candidate
    return None


def resolve_command(ctx: ToolContext, argv: list[str], cwd: Path) -> list[str]:
    """Resolve argv[0] to a real executable; prefer the repo's venv."""
    if not argv:
        raise AgentError(COMMAND_DENIED, "empty command", source="shell")
    head = argv[0]
    if os.sep in head or "/" in head:
        return list(argv)
    venv_py = _venv_python(Path(ctx.workspace))
    if head in _PY_MODULES and venv_py is not None:
        return [str(venv_py), "-m", head, *argv[1:]]
    if head in ("python", "python3") and venv_py is not None:
        return [str(venv_py), *argv[1:]]
    found = shutil.which(head)
    if found is None and head in ("python", "python3"):
        # Linux CI images sometimes expose only one of python/python3
        found = shutil.which("python3" if head == "python" else "python")
    if found:
        if found.lower().endswith((".cmd", ".bat")):
            raise AgentError(
                COMMAND_DENIED,
                f"{head} resolves to a batch shim ({found}); use the underlying executable or "
                f"'python -m {head}' instead",
                source="shell",
            )
        return [found, *argv[1:]]
    raise AgentError(
        COMMAND_DENIED,
        f"executable not found: {head}. Detected project commands are available via "
        "get_project_summary; do not guess command names.",
        source="shell",
    )


def run_command(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    argv = args.get("command") or []
    if not isinstance(argv, list) or not argv:
        return ToolResult(ok=False, error=AgentError(COMMAND_DENIED, "command must be a non-empty array", source="shell"))
    argv = [str(x) for x in argv]
    cwd = ctx.resolve(args.get("cwd", "."))
    if not cwd.is_dir():
        return ToolResult(ok=False, error=AgentError(COMMAND_DENIED, f"cwd is not a directory: {ctx.relative(cwd)}", source="shell"))
    timeout = int(args.get("timeout_seconds") or ctx.config.runtime.get("default_tool_timeout_seconds", 120))
    timeout = min(timeout, int(ctx.config.runtime.get("max_shell_timeout_seconds", 1800)))
    background = bool(args.get("background", False))
    stdin_data = args.get("stdin")
    try:
        resolved = resolve_command(ctx, argv, cwd)
    except AgentError as exc:
        return ToolResult(ok=False, error=exc)
    from ..sandbox.base import process_env

    env = {**process_env(str(ctx.workspace)), "JINLIN_SESSION": str(getattr(ctx.state, "session_id", ""))}

    if background:
        if len(ctx.processes) >= _MAX_JOBS:
            finished = [jid for jid, job in ctx.processes.items() if job["handle"].poll() is not None]
            for jid in finished:
                ctx.processes.pop(jid, None)["handle"].close()
        job_id = new_id("job")
        handle = ctx.sandbox.popen(resolved, str(cwd), env=env)
        ctx.processes[job_id] = {
            "handle": handle,
            "argv": resolved,
            "cwd": str(cwd),
            "started_at": time.time(),
            "buffer": "",
        }
        return ToolResult(
            output=(
                f"started background job {job_id} (pid={handle.pid}): {' '.join(resolved)}\n"
                f"poll with read_process_output(job_id='{job_id}'); stop with terminate_process(job_id='{job_id}')"
            ),
            extra={"job_id": job_id, "pid": handle.pid},
        )

    result = ctx.sandbox.run(
        resolved,
        str(cwd),
        timeout=timeout,
        env=env,
        stdin_data=stdin_data,
        max_output_chars=200_000,
    )
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    combined = stdout
    if stderr:
        combined += ("\n" if combined and not combined.endswith("\n") else "") + stderr
    body = [
        f"<command exit_code={result.exit_code} duration_ms={result.duration_ms} "
        f"timed_out={str(result.timed_out).lower()} engine={result.engine}>",
        "$ " + " ".join(resolved),
        "--- stdout ---",
        stdout,
        "--- stderr ---" if stderr else "",
        stderr,
        "</command>",
    ]
    output = "\n".join(x for x in body if x != "")
    if result.timed_out:
        return ToolResult(
            ok=False,
            error=AgentError(TOOL_TIMEOUT, f"command timed out after {timeout}s", source="shell"),
            output=output,
            artifact_content=combined,
            exit_code=None,
            timed_out=True,
        )
    ok = result.exit_code == 0
    return ToolResult(
        ok=ok,
        output=output,
        artifact_content=combined,
        exit_code=result.exit_code,
        extra={"argv": resolved, "engine": result.engine},
    )


def read_process_output(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    job_id = args.get("job_id") or ""
    job = ctx.processes.get(job_id)
    if job is None:
        return ToolResult(ok=False, error=AgentError("TOOL_NOT_FOUND", f"no such job: {job_id}", source="shell"))
    out, err = job["handle"].read_available()
    job["buffer"] += out
    code = job["handle"].poll()
    status = "running" if code is None else f"exited({code})"
    body = [f"<job id={job_id} status={status}>", "--- stdout ---", out]
    if err:
        body += ["--- stderr ---", err]
    body.append("</job>")
    return ToolResult(output="\n".join(body), extra={"job_id": job_id, "exit_code": code})


def terminate_process(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    job_id = args.get("job_id") or ""
    job = ctx.processes.get(job_id)
    if job is None:
        return ToolResult(ok=False, error=AgentError("TOOL_NOT_FOUND", f"no such job: {job_id}", source="shell"))
    job["handle"].terminate_tree()
    job["handle"].close()
    ctx.processes.pop(job_id, None)
    return ToolResult(output=f"terminated job {job_id}")


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(
        ToolSpec(
            name="run_command",
            description=(
                "Run a command from the workspace as an argv array (never a shell string). "
                "Returns exit code, stdout and stderr; the full output is saved as an artifact when long. "
                "Use background=true for long-running servers and poll with read_process_output."
            ),
            schema={
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": {"type": "string"}, "minLength": 1},
                    "cwd": {"type": "string", "description": "Working directory, repo-relative."},
                    "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800},
                    "background": {"type": "boolean"},
                    "stdin": {"type": "string"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            handler=run_command,
            side_effect=SideEffect.EXECUTE.value,
            idempotent=False,
            group="shell",
            timeout_seconds=1800,
            max_output_chars=20_000,
        )
    )
    registry.register(
        ToolSpec(
            name="read_process_output",
            description="Read new output from a background job started by run_command(background=true).",
            schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
            handler=read_process_output,
            group="shell",
        )
    )
    registry.register(
        ToolSpec(
            name="terminate_process",
            description="Terminate a background job and its child process tree.",
            schema={
                "type": "object",
                "properties": {"job_id": {"type": "string"}},
                "required": ["job_id"],
                "additionalProperties": False,
            },
            handler=terminate_process,
            side_effect=SideEffect.EXECUTE.value,
            idempotent=True,
            group="shell",
        )
    )
    return registry
