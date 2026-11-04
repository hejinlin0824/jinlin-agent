"""Deterministic control: path policy, command policy, approval.

This is where the harness stops trusting the model. Everything here is ordinary
code, evaluated before any side effect, and every decision is logged.
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .domain import ApprovalDecision, RiskLevel, SideEffect
from .paths import resolve_in_workspace, within as path_within
from .errors import (
    APPROVAL_DENIED,
    APPROVAL_REQUIRED,
    CAPABILITY_DENIED,
    COMMAND_DENIED,
    PATH_OUTSIDE_WORKSPACE,
    PROTECTED_FILE_ACCESS,
    SCOPE_VIOLATED,
    AgentError,
)

# --------------------------------------------------------------------------- paths

_PROTECTED_PATTERNS = [
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa",
    "id_ed25519",
    "credentials.*",
    "secrets.*",
    ".git/config",
    ".git/hooks/**",
    ".git/credentials",
    ".npmrc",
    ".pypirc",
]


@dataclass
class PathPolicy:
    workspace: Path
    follow_symlinks: bool = False

    def __post_init__(self) -> None:
        self.root = Path(os.path.abspath(self.workspace))

    # ------------------------------------------------------------------ checks

    def rel(self, path: Path) -> str:
        try:
            return Path(os.path.abspath(path)).relative_to(self.root).as_posix()
        except ValueError:
            return str(path)

    def resolve(self, raw: str) -> Path:
        return resolve_in_workspace(self.root, raw, self.follow_symlinks)

    def within(self, path: Path) -> bool:
        return path_within(self.root, path)

    def is_protected(self, rel: str) -> bool:
        rel = rel.replace("\\", "/")
        for pattern in _PROTECTED_PATTERNS:
            if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(Path(rel).name, pattern):
                return True
        return rel.startswith(".git/") or rel.startswith(".git\\")

    def check(self, raw: str, write: bool = False, delete: bool = False) -> tuple[Path | None, AgentError | None]:
        try:
            path = self.resolve(raw)
        except AgentError as exc:
            return None, exc
        rel = self.rel(path)
        if self.is_protected(rel):
            return None, AgentError(
                PROTECTED_FILE_ACCESS,
                f"{rel} is a protected path (secrets / git internals); access requires an explicit policy change",
                source="policy",
            )
        if write and rel.startswith(".git/"):
            return None, AgentError(PROTECTED_FILE_ACCESS, f"{rel}: writing to .git is not allowed", source="policy")
        return path, None


# --------------------------------------------------------------------------- commands

_GIT_READ = {"status", "diff", "log", "show", "rev-parse", "branch", "tag", "ls-files", "blame"}
_GIT_WRITE_SAFE = {"add", "checkout", "switch", "restore", "stash", "commit", "merge", "rebase"}
_RISK_RANK = {RiskLevel.LOW: 0, RiskLevel.MEDIUM: 1, RiskLevel.HIGH: 2, RiskLevel.DESTRUCTIVE: 3}
_NETWORK_EXES = {"curl", "wget", "ssh", "scp", "rsync", "ftp", "sftp", "telnet", "ncat", "nc"}
_DESTRUCTIVE_EXES = {"format", "diskpart", "reg", "shutdown", "taskkill", "bcdedit"}
_SHELL_WRAPPERS = {"cmd", "cmd.exe", "powershell", "powershell.exe", "pwsh", "pwsh.exe", "bash", "sh", "wsl"}

_TEST_EXES = {"pytest", "tox", "nox", "ruff", "mypy", "black", "flake8", "eslint", "tsc", "jest", "vitest", "npm", "pnpm", "yarn", "cargo", "go", "make", "cmake", "dotnet", "gradle", "mvn", "uvicorn", "gunicorn", "pytest-xdist"}
_SAFE_TEST_SUBCOMMANDS = {"test", "run", "check", "lint", "build", "types", "format", "--version", "--help", "list"}


@dataclass
class CommandAssessment:
    risk: RiskLevel = RiskLevel.LOW
    category: str = "read"
    reason: str = ""
    network: bool = False
    destructive: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk.value,
            "category": self.category,
            "reason": self.reason,
            "network": self.network,
            "destructive": self.destructive,
        }


def assess_command(argv: list[str]) -> CommandAssessment:
    if not argv:
        return CommandAssessment(RiskLevel.HIGH, "unknown", "empty command")
    exe = Path(argv[0]).name.lower()
    if exe.endswith(".exe"):
        exe = exe[:-4]
    args = [a.lower() for a in argv[1:]]

    if exe in _SHELL_WRAPPERS:
        return CommandAssessment(RiskLevel.HIGH, "shell", f"executes an arbitrary shell ({exe})", destructive=True)
    if exe in ("python", "python3") and args[:1] == ["-c"]:
        return CommandAssessment(RiskLevel.HIGH, "interpreter", "inline interpreter code is opaque", destructive=True)
    if exe in ("python", "python3") and args[:2] in (["-m", "pytest"], ["-m", "unittest"]):
        return CommandAssessment(RiskLevel.LOW, "test", "runs the project test suite")
    if exe in ("python", "python3") and len(args) >= 2 and args[0] == "-m" and args[1] in ("ruff", "mypy", "black", "compileall"):
        return CommandAssessment(RiskLevel.LOW, "test", f"python -m {args[1]}")
    if exe in _DESTRUCTIVE_EXES:
        return CommandAssessment(RiskLevel.DESTRUCTIVE, "system", f"system-level tool: {exe}", destructive=True)
    if exe in ("rm", "del", "rmdir", "rd") or (exe == "remove-item"):
        recursive = any(a in ("-r", "-rf", "/s", "-recurse") for a in args)
        return CommandAssessment(RiskLevel.DESTRUCTIVE if recursive else RiskLevel.HIGH, "delete", "file deletion")
    if exe in _NETWORK_EXES:
        return CommandAssessment(RiskLevel.HIGH, "network", "network access", network=True)
    if exe in ("pip", "pip3") or (exe in ("python", "python3") and args[:2] == ["-m", "pip"]):
        sub = next((a for a in args if not a.startswith("-") and a != "install" or a == "install"), "")
        if "install" in args or "uninstall" in args:
            return CommandAssessment(RiskLevel.MEDIUM, "dependency", "modifies the environment")
    if exe == "uv" and args[:1] in (["add"], ["pip"], ["sync"]):
        return CommandAssessment(RiskLevel.MEDIUM, "dependency", "modifies the environment")
    if exe in ("npm", "pnpm", "yarn"):
        if args[:1] in (["install"], ["i"], ["add"], ["ci"]):
            return CommandAssessment(RiskLevel.MEDIUM, "dependency", "installs dependencies")
        if args and args[0] in _SAFE_TEST_SUBCOMMANDS:
            return CommandAssessment(RiskLevel.LOW, "test", f"{exe} {args[0]}")
    if exe == "git":
        sub = args[0] if args else ""
        if sub in _GIT_READ:
            return CommandAssessment(RiskLevel.LOW, "git-read", f"git {sub}")
        if sub == "push":
            return CommandAssessment(RiskLevel.HIGH, "git-push", "publishes to a remote", network=True)
        if sub in ("reset",) and any(a == "--hard" for a in args):
            return CommandAssessment(RiskLevel.DESTRUCTIVE, "git-reset", "discards working tree changes", destructive=True)
        if sub == "clean":
            return CommandAssessment(RiskLevel.DESTRUCTIVE, "git-clean", "deletes untracked files", destructive=True)
        if sub in _GIT_WRITE_SAFE:
            return CommandAssessment(RiskLevel.MEDIUM, "git-write", f"git {sub} changes repository state")
        return CommandAssessment(RiskLevel.MEDIUM, "git-other", f"git {sub or ''}".strip())
    if exe in ("ls", "dir", "pwd", "echo", "cat", "type", "head", "tail", "find", "grep", "rg", "wc", "which", "where"):
        return CommandAssessment(RiskLevel.LOW, "read", f"read-only command: {exe}")
    if exe in _TEST_EXES:
        return CommandAssessment(RiskLevel.LOW, "test", f"test/build command: {exe}")
    if exe in ("mkdir", "md", "touch", "cp", "copy", "move", "mv", "rename"):
        return CommandAssessment(RiskLevel.MEDIUM, "write", f"file operation: {exe}")
    return CommandAssessment(RiskLevel.MEDIUM, "unknown", f"unrecognized executable: {exe}")


# --------------------------------------------------------------------------- approval


@dataclass
class ApprovalRequest:
    approval_id: str
    tool_call_id: str
    tool: str
    risk: str
    summary: str
    arguments: dict[str, Any]
    reason: str = ""
    impact: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


ApprovalHandler = Callable[[ApprovalRequest], tuple[str, str]]


class ApprovalManager:
    """Fail-closed approval: no answerer means denied."""

    def __init__(self, config: Any, handler: ApprovalHandler | None = None, auto_approve: bool = False) -> None:
        self.config = config
        self.handler = handler
        self.auto_approve = auto_approve
        self.session_grants: set[str] = set()
        self.count = 0

    def _mode(self, key: str, default: str = "ask") -> str:
        return str(self.config.approval.get(key, default))

    def decide(self, request: ApprovalRequest, category: str) -> tuple[str, str]:
        """Return (decision, note)."""
        if request.risk == RiskLevel.DESTRUCTIVE.value:
            return ApprovalDecision.DENIED.value, "destructive operations are denied by default"
        key = {
            "dependency": "install_dependencies",
            "git-push": "git_push",
            "git-write": "git_commit",
            "network": "network",
            "delete": "file_delete",
            "shell": "shell_high",
            "interpreter": "shell_high",
        }.get(category)
        mode = self._mode(key) if key else ("ask" if request.risk in ("medium", "high") else "auto")
        if request.risk == RiskLevel.HIGH.value and category not in ("network", "git-push", "dependency"):
            mode = self._mode("shell_high", mode)
        elif request.risk == RiskLevel.MEDIUM.value:
            mode = self._mode("shell_medium", mode)
        if mode == "deny":
            return ApprovalDecision.DENIED.value, f"policy denies {category or request.tool}"
        if mode == "auto" or self.auto_approve:
            self.count += 1
            return ApprovalDecision.APPROVED_ONCE.value, "auto-approved by policy"
        if category in self.session_grants:
            self.count += 1
            return ApprovalDecision.APPROVED_FOR_SESSION.value, "session grant"
        if self.handler is None:
            return ApprovalDecision.DENIED.value, "no approval answerer available (fail-closed)"
        self.count += 1
        decision, note = self.handler(request)
        if decision == ApprovalDecision.APPROVED_FOR_SESSION.value and category:
            self.session_grants.add(category)
        return decision, note


# --------------------------------------------------------------------------- engine


@dataclass
class PolicyDecision:
    allowed: bool
    needs_approval: bool = False
    risk: str = RiskLevel.LOW.value
    category: str = ""
    reason: str = ""
    error: AgentError | None = None
    approval_request: ApprovalRequest | None = None


class PolicyEngine:
    def __init__(
        self,
        config: Any,
        workspace: Path,
        approval_handler: ApprovalHandler | None = None,
        auto_approve: bool = False,
        read_only: bool = False,
        task_capabilities: set[str] | None = None,
        workspace_scope: Any | None = None,
    ) -> None:
        """`task_capabilities` (READ/WRITE/EXECUTE/NETWORK/DELETE) and
        `workspace_scope` come from the TaskSpec contract (v2 §9.4, ADR-006).
        None/empty means unrestricted — the single-agent path is unchanged.
        These checks run BEFORE approval and before tool.intent; prompt
        content cannot grant anything."""
        self.config = config
        self.paths = PathPolicy(workspace, follow_symlinks=bool(config.workspace.get("follow_symlinks", False)))
        self.approvals = ApprovalManager(config, approval_handler, auto_approve)
        self.read_only = read_only
        self.workspace = workspace
        self.task_capabilities = ({str(c).strip().upper() for c in task_capabilities if str(c).strip()} if task_capabilities else None)
        self.workspace_scope = workspace_scope
        self._scope_prefix_cache: dict[str, Path | None] = {}

    # ------------------------------------------------------------------ capabilities / scope

    def _cap(self, needed: str) -> bool:
        return self.task_capabilities is None or needed in self.task_capabilities

    def _capability_denial(self, needed: str, what: str) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            risk=RiskLevel.HIGH.value,
            reason=f"task capability {needed} is not granted ({what})",
            error=AgentError(
                CAPABILITY_DENIED,
                f"capability {needed} is not granted for this task: {what}",
                source="policy",
            ),
        )

    def _scope_prefix(self, prefix: str) -> Path | None:
        if prefix not in self._scope_prefix_cache:
            try:
                self._scope_prefix_cache[prefix] = resolve_in_workspace(
                    self.paths.root, prefix, self.paths.follow_symlinks
                )
            except AgentError:
                self._scope_prefix_cache[prefix] = None
        return self._scope_prefix_cache[prefix]

    def _within_scope(self, path: Path, prefixes: list[str]) -> bool:
        target = os.path.normcase(str(path))
        for prefix in prefixes:
            base = self._scope_prefix(str(prefix))
            if base is None:
                continue
            base_s = os.path.normcase(str(base))
            if target == base_s or target.startswith(base_s.rstrip(os.sep) + os.sep):
                return True
        return False

    def _scope_violation(self, raw: str, kind: str) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            risk=RiskLevel.HIGH.value,
            reason=f"{raw} is outside the task's {kind} scope",
            error=AgentError(
                SCOPE_VIOLATED,
                f"{raw} is outside the task's {kind} scope (workspace_scope)",
                source="policy",
            ),
        )

    # ------------------------------------------------------------------ tool

    def evaluate(self, tool_name: str, side_effect: str, args: dict[str, Any], tool_call_id: str = "") -> PolicyDecision:
        if self.read_only and side_effect not in (SideEffect.READ.value, SideEffect.NONE.value):
            return PolicyDecision(
                allowed=False,
                risk=RiskLevel.HIGH.value,
                reason="read-only session",
                error=AgentError(COMMAND_DENIED, "session is read-only; this operation needs write access", source="policy"),
            )
        if side_effect == SideEffect.WRITE.value and not self._cap("WRITE"):
            return self._capability_denial("WRITE", f"{tool_name} modifies files")
        if side_effect == SideEffect.EXECUTE.value and tool_name != "read_process_output" and not self._cap("EXECUTE"):
            return self._capability_denial("EXECUTE", f"{tool_name} runs processes")

        if side_effect in (SideEffect.READ.value, SideEffect.NONE.value):
            return self._check_reads(tool_name, args)

        if tool_name in ("read_file", "list_dir", "find_files", "search_text", "get_file_info", "get_git_status", "get_git_diff", "get_git_log", "get_project_summary"):
            return PolicyDecision(allowed=True)

        if side_effect == SideEffect.WRITE.value:
            return self._check_writes(tool_name, args)

        if side_effect == SideEffect.EXECUTE.value:
            return self._check_execute(tool_name, args, tool_call_id)

        return PolicyDecision(allowed=True, risk=RiskLevel.MEDIUM.value)

    # ------------------------------------------------------------------ checks

    def _check_reads(self, tool_name: str, args: dict[str, Any]) -> PolicyDecision:
        raw = args.get("path") or args.get("cwd") or "."
        if not isinstance(raw, str):
            return PolicyDecision(allowed=True)
        _path, error = self.paths.check(raw, write=False)
        if error:
            return PolicyDecision(allowed=False, risk=RiskLevel.HIGH.value, error=error, reason=error.message)
        read_scope = list(getattr(self.workspace_scope, "read", None) or []) if self.workspace_scope else []
        if read_scope and _path is not None and not self._within_scope(_path, read_scope):
            return self._scope_violation(raw, "read")
        return PolicyDecision(allowed=True)

    def _check_writes(self, tool_name: str, args: dict[str, Any]) -> PolicyDecision:
        if not self.config.workspace.get("allow_write", True):
            return PolicyDecision(
                allowed=False,
                risk=RiskLevel.HIGH.value,
                error=AgentError(COMMAND_DENIED, "workspace writes are disabled in config", source="policy"),
                reason="writes disabled",
            )
        write_scope = list(getattr(self.workspace_scope, "write", None) or []) if self.workspace_scope else []
        candidates: list[str] = []
        path = args.get("path")
        if isinstance(path, str):
            candidates.append(path)
        if tool_name == "apply_patch":
            candidates.extend(_patch_paths(str(args.get("patch") or "")))
        for raw in candidates:
            resolved, error = self.paths.check(raw, write=True)
            if error:
                return PolicyDecision(allowed=False, risk=RiskLevel.HIGH.value, error=error, reason=error.message)
            if write_scope and resolved is not None and not self._within_scope(resolved, write_scope):
                return self._scope_violation(raw, "write")
        if not self.config.workspace.get("allow_delete", False) and tool_name == "delete_file":
            return PolicyDecision(
                allowed=False,
                risk=RiskLevel.HIGH.value,
                error=AgentError(COMMAND_DENIED, "deleting files is disabled in config", source="policy"),
                reason="delete disabled",
            )
        return PolicyDecision(allowed=True, risk=RiskLevel.LOW.value, category="write")

    def _check_execute(self, tool_name: str, args: dict[str, Any], tool_call_id: str) -> PolicyDecision:
        if tool_name in ("read_process_output", "terminate_process"):
            return PolicyDecision(allowed=True, risk=RiskLevel.LOW.value, category="process")
        argv = args.get("command") or []
        if not isinstance(argv, list) or not argv:
            return PolicyDecision(
                allowed=False,
                risk=RiskLevel.HIGH.value,
                error=AgentError(COMMAND_DENIED, "command must be a non-empty argv array", source="policy"),
            )
        cwd_raw = args.get("cwd", ".")
        if isinstance(cwd_raw, str):
            try:
                self.paths.resolve(cwd_raw)
            except AgentError as exc:
                return PolicyDecision(allowed=False, risk=RiskLevel.HIGH.value, error=exc, reason=exc.message)
        assessment = assess_command([str(x) for x in argv])
        if assessment.network and not self._cap("NETWORK"):
            return self._capability_denial("NETWORK", f"command reaches the network ({assessment.reason})")
        if assessment.destructive and not self._cap("DELETE"):
            return self._capability_denial("DELETE", f"command is destructive ({assessment.reason})")
        mode_decision = self._risk_mode(assessment)
        summary = " ".join(str(x) for x in argv)[:300]
        request = ApprovalRequest(
            approval_id="",
            tool_call_id=tool_call_id,
            tool=tool_name,
            risk=assessment.risk.value,
            summary=summary,
            arguments={"command": argv, "cwd": args.get("cwd", ".")},
            reason=assessment.reason,
            impact=[assessment.category],
        )
        if mode_decision == "auto":
            return PolicyDecision(allowed=True, risk=assessment.risk.value, category=assessment.category, reason=assessment.reason)
        if mode_decision == "deny":
            return PolicyDecision(
                allowed=False,
                risk=assessment.risk.value,
                category=assessment.category,
                reason=assessment.reason,
                error=AgentError(
                    COMMAND_DENIED,
                    f"command denied by policy ({assessment.category}: {assessment.reason})",
                    source="policy",
                ),
            )
        return PolicyDecision(
            allowed=True,
            needs_approval=True,
            risk=assessment.risk.value,
            category=assessment.category,
            reason=assessment.reason,
            approval_request=request,
        )

    def _risk_mode(self, assessment: CommandAssessment) -> str:
        if assessment.risk == RiskLevel.DESTRUCTIVE:
            return self._approval("shell_destructive", "deny")
        if assessment.risk == RiskLevel.HIGH:
            key = "git_push" if assessment.category == "git-push" else ("network" if assessment.network else "shell_high")
            return self._approval(key, "ask")
        if assessment.risk == RiskLevel.MEDIUM:
            if assessment.category == "dependency":
                return self._approval("install_dependencies", "ask")
            if assessment.category == "git-write":
                return self._approval("git_commit", "ask")
            return self._approval("shell_medium", "ask")
        return "auto"

    def _approval(self, key: str, default: str) -> str:
        return str(self.config.approval.get(key, default))


def _patch_paths(patch_text: str) -> list[str]:
    out: list[str] = []
    for line in patch_text.splitlines():
        stripped = line.strip()
        for prefix in ("*** Add File:", "*** Update File:", "*** Delete File:"):
            if stripped.startswith(prefix):
                out.append(stripped[len(prefix):].strip())
    return out
