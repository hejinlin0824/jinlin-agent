"""Project detection: one compact summary the model gets at startup and on demand."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import AgentError
from .base import ToolContext, ToolResult, ToolSpec
from .registry import ToolRegistry

_SIGNALS = {
    "python": ["pyproject.toml", "setup.py", "requirements.txt", "tox.ini", "pytest.ini", "setup.cfg"],
    "node": ["package.json", "pnpm-lock.yaml", "yarn.lock", "package-lock.json", "tsconfig.json"],
    "rust": ["Cargo.toml"],
    "go": ["go.mod"],
    "java": ["pom.xml", "build.gradle", "build.gradle.kts"],
    "make": ["Makefile", "makefile"],
    "docker": ["Dockerfile", "docker-compose.yml", "compose.yml"],
}


def _read(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _detect_commands(root: Path, types: list[str]) -> list[list[str]]:
    commands: list[list[str]] = []
    pyproject = root / "pyproject.toml"
    has_pytest = False
    if pyproject.exists():
        body = _read(pyproject)
        has_pytest = "pytest" in body
        if "[tool.poetry]" in body:
            commands.append(["poetry", "run", "pytest", "-q"])
    if (root / "pytest.ini").exists() or (root / "tests").is_dir() or has_pytest:
        commands.append(["pytest", "-q"])
    if (root / "Makefile").exists():
        make_body = _read(root / "Makefile").lower()
        if "test:" in make_body:
            commands.append(["make", "test"])
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(_read(pkg, 20000))
        except json.JSONDecodeError:
            data = {}
        scripts = data.get("scripts") or {}
        for name in ("test", "lint", "build"):
            if name in scripts:
                commands.append(["npm", "run", name])
    if (root / "Cargo.toml").exists():
        commands.append(["cargo", "test"])
    if (root / "go.mod").exists():
        commands.append(["go", "test", "./..."])
    return commands[:6]


def get_project_summary(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    root = ctx.workspace
    types = [name for name, files in _SIGNALS.items() if any((root / f).exists() for f in files)]
    instructions = [f for f in ctx.config.project.get("instruction_files", []) if (root / f).exists()]
    top: list[str] = []
    try:
        for entry in sorted(root.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))[:40]:
            if entry.name in set(ctx.config.project.get("ignore_dirs", [])):
                continue
            top.append(entry.name + ("/" if entry.is_dir() else ""))
    except OSError:
        pass

    git: dict[str, Any] = {"repo": False}
    res = ctx.sandbox.run(["git", "status", "--porcelain=v1", "-b"], str(root), timeout=30)
    if res.exit_code == 0:
        first = (res.stdout or "").splitlines()[0] if res.stdout else ""
        head = ctx.sandbox.run(["git", "rev-parse", "--short", "HEAD"], str(root), timeout=30)
        git = {
            "repo": True,
            "branch_line": first,
            "head": (head.stdout or "").strip() if head.exit_code == 0 else "",
            "dirty": bool([l for l in (res.stdout or "").splitlines()[1:] if l.strip()]),
        }

    summary = {
        "root": str(root),
        "project_types": types,
        "instructions": instructions,
        "candidate_commands": [" ".join(c) for c in _detect_commands(root, types)],
        "top_level": top,
        "git": git,
        "platform": {"os": "windows" if __import__("sys").platform == "win32" else "posix"},
    }
    body = json.dumps(summary, ensure_ascii=False, indent=2)
    return ToolResult(output=f"<project_summary>\n{body}\n</project_summary>", extra={"summary": summary})


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(
        ToolSpec(
            name="get_project_summary",
            description=(
                "One-call project overview: detected project types, instruction files, top-level layout, "
                "candidate test/build commands and git state. Call this before guessing how to test the repo."
            ),
            schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=get_project_summary,
            group="project",
            idempotent=True,
        )
    )
    return registry
