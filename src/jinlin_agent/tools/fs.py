"""Filesystem read tools.

Every response carries provenance (path, hash, line range) so the model can
reference what it saw and the harness can enforce read-before-write.
"""

from __future__ import annotations

import fnmatch
import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterator

from ..errors import (
    PATH_OUTSIDE_WORKSPACE,
    PROTECTED_FILE_ACCESS,
    TOOL_EXECUTION_FAILED,
    AgentError,
)
from .base import ToolContext, ToolResult, ToolSpec
from .registry import ToolRegistry

_TEXT_EXTRA = 20_000


# --------------------------------------------------------------------------- helpers


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def file_bytes(path: Path) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def decode_text(data: bytes) -> tuple[str, str, bool]:
    """Return (text, encoding, had_bom). Prefers UTF-8, tolerates broken bytes."""
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig"), "utf-8", True
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace"), "utf-8?", False


def is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


def ignore_dirs(ctx: ToolContext) -> set[str]:
    return set(ctx.config.project.get("ignore_dirs", []))


def iter_repo_files(ctx: ToolContext, root: Path) -> Iterator[Path]:
    ignores = ignore_dirs(ctx)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignores]
        for name in filenames:
            p = Path(dirpath) / name
            try:
                if p.is_symlink() and not ctx.config.workspace.get("follow_symlinks", False):
                    continue
            except OSError:
                continue
            yield p


def _header(path: str, lines: str, sha: str, truncated: bool, encoding: str = "utf-8") -> str:
    return (
        f'<file path="{path}" lines="{lines}" sha256="{sha}" '
        f'encoding="{encoding}" truncated="{str(truncated).lower()}">'
    )


# --------------------------------------------------------------------------- tools


def read_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = ctx.resolve(args["path"])
    rel = ctx.relative(path)
    if not path.exists() or not path.is_file():
        return ToolResult(ok=False, error=AgentError("FILE_NOT_FOUND", f"no such file: {rel}", source="tool"))
    size = path.stat().st_size
    if size > int(ctx.config.workspace.get("max_file_bytes", 1_048_576)):
        return ToolResult(
            ok=False,
            error=AgentError(
                "FILE_TOO_LARGE",
                f"{rel} is {size} bytes; use read_file with offset/limit or search_text",
                source="tool",
            ),
        )
    data = file_bytes(path)
    if is_binary(data):
        return ToolResult(
            ok=False,
            error=AgentError("BINARY_FILE", f"{rel} is binary; not shown as text", source="tool"),
        )
    text, encoding, _bom = decode_text(data)
    all_lines = text.splitlines()
    offset = max(1, int(args.get("offset", 1)))
    limit = int(args.get("limit", 200))
    limit = max(1, min(limit, 2000))
    total = len(all_lines)
    start = min(offset - 1, total)
    end = min(start + limit, total)
    chunk = all_lines[start:end]
    digest = sha256_bytes(data)
    rel_posix = rel
    body = "\n".join(chunk)
    out = f"{_header(rel_posix, f'{start + 1}-{end}/{total}', digest, end < total, encoding)}\n{body}\n</file>"
    return ToolResult(
        output=out,
        read_hashes={rel_posix: digest},
        extra={"lines": total, "size": size, "encoding": encoding, "sha256": digest, "range": [start + 1, end]},
    )


def list_dir(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = ctx.resolve(args.get("path", "."))
    rel = ctx.relative(path)
    if not path.exists() or not path.is_dir():
        return ToolResult(ok=False, error=AgentError("NOT_A_DIRECTORY", f"not a directory: {rel}", source="tool"))
    max_entries = int(args.get("max_entries", 200))
    entries: list[str] = []
    try:
        for entry in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.name in ignore_dirs(ctx):
                continue
            suffix = "/" if entry.is_dir() else ""
            entries.append(f"{entry.name}{suffix}")
    except OSError as exc:
        return ToolResult(ok=False, error=AgentError(TOOL_EXECUTION_FAILED, str(exc), source="tool"))
    shown = entries[:max_entries]
    out_lines = [f'<dir path="{rel}" entries="{len(entries)}">']
    out_lines.extend(shown)
    if len(entries) > len(shown):
        out_lines.append(f"... ({len(entries) - len(shown)} more)")
    out_lines.append("</dir>")
    return ToolResult(output="\n".join(out_lines), extra={"entries": len(entries)})


def find_files(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = args.get("pattern") or "*"
    root = ctx.resolve(args.get("path", "."))
    rel_root = ctx.relative(root)
    max_results = int(args.get("max_results", 200))
    matches: list[str] = []
    for p in iter_repo_files(ctx, root):
        rel = ctx.relative(p)
        if fnmatch.fnmatch(p.name, pattern) or fnmatch.fnmatch(rel, pattern):
            matches.append(rel)
            if len(matches) >= max_results:
                break
    matches.sort()
    out = [f'<files pattern="{pattern}" root="{rel_root}" count="{len(matches)}">']
    out.extend(matches)
    out.append("</files>")
    return ToolResult(output="\n".join(out), extra={"count": len(matches)})


def search_text(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = args.get("pattern") or ""
    if not pattern:
        return ToolResult(ok=False, error=AgentError("TOOL_SCHEMA_ERROR", "pattern is required", source="tool"))
    root = ctx.resolve(args.get("path", "."))
    file_glob = args.get("glob")
    case_sensitive = bool(args.get("case_sensitive", False))
    max_matches = int(args.get("max_matches", 80))
    context_lines = int(args.get("context_lines", 0))
    try:
        flags = 0 if case_sensitive else re.IGNORECASE
        regex = re.compile(pattern, flags)
    except re.error as exc:
        return ToolResult(ok=False, error=AgentError("TOOL_SCHEMA_ERROR", f"bad regex: {exc}", source="tool"))
    results: list[str] = []
    files_hit = 0
    for p in iter_repo_files(ctx, root):
        rel = ctx.relative(p)
        if file_glob and not fnmatch.fnmatch(p.name, file_glob):
            continue
        try:
            if p.stat().st_size > int(ctx.config.workspace.get("max_file_bytes", 1_048_576)):
                continue
            data = file_bytes(p)
        except OSError:
            continue
        if is_binary(data):
            continue
        text, _enc, _bom = decode_text(data)
        lines = text.splitlines()
        hit = False
        for i, line in enumerate(lines):
            if regex.search(line):
                hit = True
                files_hit += 1 if i == 0 else 0
                if context_lines > 0:
                    lo = max(0, i - context_lines)
                    hi = min(len(lines), i + context_lines + 1)
                    for j in range(lo, hi):
                        prefix = ":" if j == i else "-"
                        results.append(f"{rel}{prefix}{j + 1}{prefix} {lines[j]}")
                else:
                    results.append(f"{rel}:{i + 1}: {line}")
                if len(results) >= max_matches:
                    break
        if len(results) >= max_matches:
            break
    out = [f'<search pattern="{pattern}" matches="{len(results)}">']
    out.extend(results)
    if len(results) >= max_matches:
        out.append("... (truncated; narrow the pattern or raise max_matches)")
    out.append("</search>")
    return ToolResult(output="\n".join(out), extra={"matches": len(results)})


def get_file_info(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path = ctx.resolve(args["path"])
    rel = ctx.relative(path)
    if not path.exists():
        return ToolResult(ok=False, error=AgentError("FILE_NOT_FOUND", f"no such path: {rel}", source="tool"))
    st = path.stat()
    data = file_bytes(path) if path.is_file() else b""
    text, encoding, _ = decode_text(data) if data else ("", "utf-8", False)
    out = (
        f'<info path="{rel}" type="{"dir" if path.is_dir() else "file"}" '
        f'size="{st.st_size}" mtime="{int(st.st_mtime)}" '
        f'sha256="{sha256_bytes(data)}" lines="{len(text.splitlines())}" '
        f'encoding="{encoding}" binary="{str(is_binary(data)).lower()}">\n</info>'
    )
    return ToolResult(output=out, read_hashes={rel: sha256_bytes(data)} if path.is_file() else {})


# --------------------------------------------------------------------------- registration


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(
        ToolSpec(
            name="read_file",
            description=(
                "Read a UTF-8 text file from the workspace. Returns the file with a provenance "
                "header (path, line range, sha256). Always read a file before editing it; edits to "
                "files you have not read are rejected by policy. Use offset/limit for large files."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repository-relative path."},
                    "offset": {"type": "integer", "minimum": 1, "description": "1-based first line."},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 2000, "description": "Max lines (default 200)."},
                },
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=read_file,
            group="filesystem",
            max_output_chars=30_000,
        )
    )
    registry.register(
        ToolSpec(
            name="list_dir",
            description="List one directory level (repo-relative). Directories end with '/'.",
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path, default '.'."},
                    "max_entries": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "additionalProperties": False,
            },
            handler=list_dir,
            group="filesystem",
            max_output_chars=12_000,
        )
    )
    registry.register(
        ToolSpec(
            name="find_files",
            description="Find files by glob (e.g. '*.py', '**/test_*.py'), honoring ignore directories.",
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "description": "Search root, default '.'."},
                    "max_results": {"type": "integer", "minimum": 1, "maximum": 1000},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=find_files,
            group="filesystem",
        )
    )
    registry.register(
        ToolSpec(
            name="search_text",
            description=(
                "Regex search across repository text files. Returns 'path:line: text'. "
                "Narrow patterns when there are many matches."
            ),
            schema={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regular expression."},
                    "path": {"type": "string"},
                    "glob": {"type": "string", "description": "Only files matching this glob."},
                    "case_sensitive": {"type": "boolean"},
                    "context_lines": {"type": "integer", "minimum": 0, "maximum": 5},
                    "max_matches": {"type": "integer", "minimum": 1, "maximum": 500},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
            handler=search_text,
            group="filesystem",
        )
    )
    registry.register(
        ToolSpec(
            name="get_file_info",
            description="Metadata for a file or directory: size, mtime, sha256, line count, binary flag.",
            schema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
            handler=get_file_info,
            group="filesystem",
        )
    )
    return registry
