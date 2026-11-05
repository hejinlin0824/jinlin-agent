"""Workspace Manager + Integration Coordinator (v2.0 WP-V2.0-5; ADR-005).

Git worktrees give every worker an independent checkout on agent/task-<id>.
Worktrees are created under the harness home (~/.jinlin-agent/worktrees/...):
the user's repository is never polluted, and the worker's workspace boundary
becomes the worktree itself. A worktree is VERSION isolation, not a security
boundary — the OS sandbox still applies to the worker's workspace (ADR-005).

The Integration Coordinator owns merges:
  1. commit the worker's worktree with a harness identity (no dependency on
     the user's git config);
  2. detect a stale base and rebase onto the integration branch first;
  3. merge into agent/integration-<root>;
  4. PAUSE on conflict — the merge is aborted, the conflicting files are
     named in an integration.conflict message, nothing is overwritten
     (v2 §9.3);
  5. run verification on the integration tree after every merge and roll the
     merge back when it fails;
  6. the stable branch is never touched here — release is the owner's, after
     the root completion gate (v2 §14-WP5 acceptance).

All git access is argv-based subprocess (no shell=True); these operations are
harness infrastructure driven by the Supervisor, not model-invoked tools, so
they bypass the tool policy pipeline by design and are logged in the task
event stream instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import AgentError, GIT_OPERATION_FAILED
from .ledger import TaskLedger
from .sandbox import create_sandbox
from .sandbox.base import process_env
from .taskmodel import TaskMessage, new_message_id
from .verify import detect_project, parse_test_output

_IDENTITY = {
    "GIT_AUTHOR_NAME": "jinlin-agent",
    "GIT_AUTHOR_EMAIL": "jinlin-agent@local",
    "GIT_COMMITTER_NAME": "jinlin-agent",
    "GIT_COMMITTER_EMAIL": "jinlin-agent@local",
}


def _git(repo: Path, *args: Any, env_extra: dict[str, str] | None = None, check: bool = True) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.update(_IDENTITY)
    env.update({k: v for k, v in (env_extra or {}).items()})
    res = subprocess.run(
        ["git", *[str(a) for a in args]],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
    )
    if check and res.returncode != 0:
        raise AgentError(
            GIT_OPERATION_FAILED,
            f"git {' '.join(str(a) for a in args)} failed: {(res.stderr or res.stdout or '').strip()[:400]}",
            source="worktree",
        )
    return res


def _with_confcutdir(command: list[str], root: Path) -> list[str]:
    """Constrain pytest to the integration tree.

    pytest 9's perform_collect walks ALL ancestor directories of its args
    (confcutdir defaults to None = unbounded) to collect them as packages.
    Under the L1 sandbox the integration tree's PARENT is deliberately not
    readable (containment), and that ancestor walk dies with PermissionError
    before a single test runs. Pinning confcutdir to the tree keeps every
    read inside the containment boundary; non-pytest commands are untouched
    (PYTEST_ADDOPTS-style env tricks would also work, but an explicit argv
    is auditable)."""
    if not command:
        return command
    prog = Path(str(command[0])).name.lower()
    prog = prog[:-4] if prog.endswith(".exe") else prog
    pytest_token = -1
    if "pytest" in prog:
        pytest_token = 0
    elif prog in ("python", "python3") and len(command) >= 3 and str(command[1]) == "-m" and "pytest" in [str(x).lower() for x in command[2:4]]:
        pytest_token = 3
    if pytest_token < 0:
        return command
    out = list(command)
    out.insert(pytest_token + 1, "--confcutdir")
    out.insert(pytest_token + 2, str(root))
    return out


@dataclass
class WorkerWorkspace:
    task_id: str
    path: Path
    branch: str
    base_commit: str


class WorkspaceManager:
    def __init__(self, repo: Path | str, config: Any, ledger: TaskLedger | None = None) -> None:
        self.repo = Path(repo).resolve()
        self.config = config
        self.ledger = ledger
        # Worktrees live INSIDE the workspace under .jinlin/ (same place v1
        # already keeps config/locks/trash evidence). Containment reason: the
        # L1 sandbox grants its write ACE on the workspace subtree, so every
        # ANCESTOR of a worktree stays readable by the restricted token —
        # pytest 9 stats the parent of every collected node (conftest probe)
        # and dies with PermissionError under an unreadable parent. Harness-
        # home placement put worktrees behind an unreadable parent and broke
        # integration verification (WP-R2, CI/first-principles caught).
        self.home = self.repo / ".jinlin" / "worktrees"
        self._worktrees: dict[str, WorkerWorkspace] = {}
        self._coordinators: dict[str, IntegrationCoordinator] = {}

    @staticmethod
    def is_git_repo(path: Path | str) -> bool:
        """True only when ``path`` IS a repository/worktree root — not merely
        a directory inside someone else's repo. Attaching the manager to a
        subdirectory would create ``agent/*`` branches and worktrees on the
        OUTER repository (a checkout we do not own); the WP-F2 dry-run caught
        exactly that against the harness's own checkout."""
        try:
            root = Path(path).resolve(strict=True)
        except OSError:
            return False
        res = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=30,
        )
        if res.returncode != 0:
            return False
        try:
            toplevel = Path(res.stdout.strip()).resolve()
        except OSError:
            return False
        return toplevel == root

    def create_worker_workspace(self, task_id: str, base_commit: str | None = None) -> WorkerWorkspace:
        path = self.home / task_id
        if path.exists():
            self.cleanup_worker_workspace(task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        branch = f"agent/task-{task_id}"
        if base_commit is None:
            base_commit = _git(self.repo, "rev-parse", "HEAD").stdout.strip()
        _git(self.repo, "worktree", "add", "-b", branch, str(path), base_commit)
        ws = WorkerWorkspace(task_id=task_id, path=path, branch=branch, base_commit=base_commit)
        self._worktrees[task_id] = ws
        return ws

    def get(self, task_id: str) -> WorkerWorkspace | None:
        return self._worktrees.get(task_id)

    def workspaces(self) -> list[WorkerWorkspace]:
        return list(self._worktrees.values())

    def cleanup_worker_workspace(self, task_id: str) -> None:
        ws = self._worktrees.pop(task_id, None)
        if ws is None:
            return
        if ws.path.exists():
            try:
                _git(self.repo, "worktree", "remove", "--force", str(ws.path))
            except AgentError:
                shutil.rmtree(ws.path, ignore_errors=True)
                _git(self.repo, "worktree", "prune")

    def integrate(self, task_id: str, root_id: str) -> dict[str, Any]:
        """Commit + merge one worker workspace into the root's integration
        branch (creating the coordinator on first use)."""
        ws = self._worktrees.get(task_id)
        if ws is None:
            raise AgentError(GIT_OPERATION_FAILED, f"no worktree for task {task_id}", source="worktree")
        coordinator = self._coordinators.get(root_id)
        if coordinator is None:
            coordinator = IntegrationCoordinator(
                self.repo, self.config, self.ledger, root_id=root_id, manager=self
            )
            self._coordinators[root_id] = coordinator
        return coordinator.integrate(task_id, ws)

    def integration_path(self, root_id: str | None = None) -> Path:
        if root_id is not None:
            coordinator = self._coordinators.get(root_id)
        else:
            coordinators = list(self._coordinators.values())
            if len(coordinators) != 1:
                raise AgentError(
                    GIT_OPERATION_FAILED,
                    "integration_path() needs a root_id when zero or several integration trees exist",
                    source="worktree",
                )
            coordinator = coordinators[0]
        return coordinator.integration_path()

    def cleanup_all(self) -> None:
        for coordinator in self._coordinators.values():
            coordinator.cleanup()
        self._coordinators.clear()
        for task_id in list(self._worktrees):
            self.cleanup_worker_workspace(task_id)
        _git(self.repo, "worktree", "prune")

    # ------------------------------------------------------------------ recovery (WP-R5)

    def recover(self, ledger: TaskLedger | None, root_id: str) -> dict[str, Any]:
        """Rebuild in-memory worktree mappings after a process restart.

        Live worktrees under the manager home with a branch agent/task-<id>
        whose ledger worker is still non-terminal are ADOPTED (integration can
        finish what the restart interrupted). Anything else under the home —
        worktrees of terminal workers, unrecognized directories — is reported
        as an ORPHAN and never auto-deleted: a deliverable's only copy is not
        trash (WP-R5.7)."""
        adopted: list[WorkerWorkspace] = []
        orphans: list[dict[str, str]] = []
        seen_worktree_paths: set[Path] = set()
        listing = _git(self.repo, "worktree", "list", "--porcelain").stdout
        for entry in listing.split("\n\n"):
            lines = [line for line in entry.split("\n") if line.strip()]
            path: Path | None = None
            branch: str | None = None
            for line in lines:
                if line.startswith("worktree "):
                    path = Path(line[len("worktree "):])
                elif line.startswith("branch "):
                    branch = line[len("branch "):].replace("refs/heads/", "")
            if path is None or branch is None:
                continue
            if path.parent != self.home:
                continue  # external worktrees are not ours to manage
            seen_worktree_paths.add(path.resolve())
            if path.name.startswith("integration-"):
                continue  # the integration tree is coordinator-managed
            task_id = path.name
            expected_branch = f"agent/task-{task_id}"
            if branch != expected_branch:
                orphans.append({"path": str(path), "reason": f"unexpected branch {branch!r}"})
                continue
            rows = [w for w in ledger.list_workers(task_id) if w.get("branch") == branch] if ledger is not None else []
            worker_row = rows[-1] if rows else None
            task_row = ledger.get_task(task_id) if ledger is not None else None
            integration_done = task_row is not None and task_row.get("integration") == "integrated"
            adoptable = worker_row is not None and (
                worker_row["status"] not in ("completed", "failed", "cancelled")
                # completed-but-unintegrated: exactly what a restart interrupted
                or (worker_row["status"] == "completed" and not integration_done)
            )
            if adoptable:
                base = worker_row.get("base_commit", "")
                ws = WorkerWorkspace(task_id=task_id, path=path, branch=branch, base_commit=base)
                self._worktrees[task_id] = ws
                adopted.append(ws)
            else:
                orphans.append(
                    {
                        "path": str(path),
                        "reason": "worktree of a terminal/integrated worker; inspect before deleting",
                    }
                )
        # plain directories under the home are not git worktrees, so the
        # porcelain listing never shows them — scan them too (report only)
        if self.home.exists():
            for child in self.home.iterdir():
                if child.name.startswith("integration-"):
                    continue
                if child.resolve() in seen_worktree_paths:
                    continue
                if child.name.startswith("task_"):
                    orphans.append(
                        {
                            "path": str(child),
                            "reason": "unrecognized directory (not a registered worktree); inspect before deleting",
                        }
                    )
        return {"adopted": adopted, "orphans": orphans}

    # ------------------------------------------------------------------ sandbox plumbing (WP-R2)

    def make_sandbox(self, workspace: Path | str) -> Any:
        """Create a sandbox for a path inside the workspace and grant the
        sandbox SID read/traverse on the ancestor chain up to the repository
        root. Needed because the L1 write ACE covers only the workspace
        subtree, while contained pytest runs stat ancestors of every
        collected node. Traverse is read-only by contract (Sandbox.
        grant_traverse never grants write)."""
        sandbox = create_sandbox(workspace, self.config)
        self._grant_traverse_chain(sandbox, Path(workspace))
        return sandbox

    def _grant_traverse_chain(self, sandbox: Any, target: Path) -> None:
        grant = getattr(sandbox, "grant_traverse", None)
        if not callable(grant):
            return
        repo_root = Path(os.path.normcase(os.path.abspath(self.repo)))
        segments: list[Path] = []
        for parent in Path(target).resolve().parents:
            if parent == repo_root:
                segments.append(parent)
                break
            segments.append(parent)
            if len(segments) >= 8:  # containment sanity cap
                break
        for segment in reversed(segments):
            try:
                grant(segment)
            except Exception:
                pass  # best-effort: verification will fail closed if it matters

    def sandbox_factory(self) -> Any:
        """Worker-runtime sandbox factory bound to this manager (grants the
        traverse chain, so worker-session verification works under L1 too)."""
        return lambda ws: self.make_sandbox(ws)


class IntegrationCoordinator:
    def __init__(
        self,
        repo: Path | str,
        config: Any,
        ledger: TaskLedger | None,
        root_id: str,
        manager: WorkspaceManager,
        sandbox_factory: Any | None = None,
    ) -> None:
        self.repo = Path(repo).resolve()
        self.config = config
        self.ledger = ledger
        self.root_id = root_id
        self.manager = manager
        # WP-R2: the integration tree's tests execute UNTRUSTED repository
        # code — they must run through the Sandbox abstraction, never through
        # a bare subprocess. Git plumbing stays argv-based by design (see the
        # module docstring: management operations are a different trust
        # boundary, and the reviewer's task book draws the same line).
        self._sandbox_factory = sandbox_factory or (lambda ws: self.manager.make_sandbox(ws))
        self.integration_branch = f"agent/integration-{root_id}"
        self._path = manager.home / f"integration-{root_id}"
        self._ensure_integration_tree()

    # ------------------------------------------------------------------ setup

    def _ensure_integration_tree(self) -> None:
        branches = _git(self.repo, "branch", "--list", self.integration_branch).stdout.strip()
        if not branches:
            _git(self.repo, "branch", self.integration_branch, _git(self.repo, "rev-parse", "HEAD").stdout.strip())
        if not self._path.exists():
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _git(self.repo, "worktree", "add", str(self._path), self.integration_branch)

    def integration_path(self) -> Path:
        return self._path

    # ------------------------------------------------------------------ integration

    def integrate(self, task_id: str, ws: WorkerWorkspace) -> dict[str, Any]:
        commit_id = self._commit_worker_worktree(ws)
        if commit_id == ws.base_commit:
            # The worker delivered nothing (worktree still at base): there is
            # no diff to merge, and re-verifying the unchanged tree with the
            # project suite would test work nobody did.
            return {
                "status": "not_needed",
                "task_id": task_id,
                "commit": commit_id,
                "rebased": False,
                "verification": None,
            }
        int_head = _git(self._path, "rev-parse", "HEAD").stdout.strip()

        rebased = False
        if ws.base_commit != int_head:
            behind = _git(self._path, "merge-base", "--is-ancestor", ws.base_commit, int_head, check=False).returncode == 0
            diverged = not behind
            if behind or diverged:
                rebase = _git(ws.path, "rebase", self.integration_branch, check=False)
                if rebase.returncode != 0:
                    files = _git(ws.path, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
                    _git(ws.path, "rebase", "--abort", check=False)
                    return self._record_conflict(task_id, files or ["<rebase conflict>"])
                rebased = True

        merge_args = ["merge", "--ff-only", ws.branch] if rebased and behind else ["merge", "--no-ff", "-m", f"integrate {task_id}", ws.branch]
        merge = _git(self._path, *merge_args, check=False)
        if merge.returncode != 0:
            files = _git(self._path, "diff", "--name-only", "--diff-filter=U", check=False).stdout.split()
            _git(self._path, "merge", "--abort", check=False)
            return self._record_conflict(task_id, files or ["<merge conflict>"])

        verification = self._verify()
        if self.ledger is not None:
            self.ledger.emit(
                self.root_id,
                "integration.verified",
                {"task_id": task_id, "commit": commit_id, **verification},
            )
        if not verification["passed"]:
            _git(self._path, "reset", "--hard", int_head)
            return {
                "status": "verification_failed",
                "task_id": task_id,
                "commit": commit_id,
                "rebased": rebased,
                "verification": verification,
            }
        return {
            "status": "integrated",
            "task_id": task_id,
            "commit": commit_id,
            "rebased": rebased,
            "verification": verification,
        }

    def _commit_worker_worktree(self, ws: WorkerWorkspace) -> str:
        _git(ws.path, "add", "-A")
        # hygiene: never ship build artifacts even if the repo lacks a .gitignore
        _git(
            ws.path,
            "reset",
            "--quiet",
            "--",
            ":(glob)**/__pycache__/**",
            ":(glob)**/*.pyc",
            ":(glob)**/*.pyo",
            ":(glob)**/.pytest_cache/**",
            check=False,
        )
        nothing = _git(ws.path, "diff", "--cached", "--quiet", check=False).returncode == 0
        if nothing:
            return _git(ws.path, "rev-parse", "HEAD").stdout.strip()
        _git(ws.path, "commit", "-q", "-m", f"task {ws.task_id}: deliverable")
        return _git(ws.path, "rev-parse", "HEAD").stdout.strip()

    def _record_conflict(self, task_id: str, conflict_files: list[str]) -> dict[str, Any]:
        if self.ledger is not None:
            self.ledger.record_message(
                TaskMessage(
                    message_id=new_message_id(),
                    root_task_id=self.root_id,
                    task_id=task_id,
                    message_type="integration.conflict",
                    payload={"conflict_files": conflict_files, "integration_branch": self.integration_branch},
                )
            )
        return {
            "status": "conflict",
            "task_id": task_id,
            "conflict_files": conflict_files,
            "integration_branch": self.integration_branch,
        }

    def _verify(self) -> dict[str, Any]:
        """Run the project's own tests on the integration tree THROUGH the
        sandbox (WP-R2: untrusted repository code, fail-closed when the
        sandbox cannot be built or refuses; no subprocess fallback anywhere).
        'Affected only' targeting arrives with diff-aware planning."""
        project = detect_project(self._path, self.config)
        if not project.test_commands:
            return {"passed": True, "command": "", "exit_code": None, "note": "no test command detected", "sandbox": {}}
        try:
            sandbox = self._sandbox_factory(self._path)
        except Exception as exc:
            return {
                "passed": False,
                "command": " ".join(project.test_commands[0]),
                "exit_code": None,
                "note": f"sandbox unavailable, fail-closed: {type(exc).__name__}: {exc}",
                "sandbox": {},
            }
        self.manager._grant_traverse_chain(sandbox, self._path)
        command = project.test_commands[0]
        command = _with_confcutdir(command, self._path)
        sandbox_info = {
            "level": getattr(sandbox, "level", "L0"),
            "engine": getattr(sandbox, "engine", "none"),
        }
        try:
            res = sandbox.run(
                command,
                cwd=str(self._path),
                timeout=int(self.config.runtime.get("max_shell_timeout_seconds", 1800)),
                env=process_env(str(self._path)),
            )
        except Exception as exc:
            return {
                "passed": False,
                "command": " ".join(command),
                "exit_code": None,
                "note": f"sandbox refused execution, fail-closed: {type(exc).__name__}: {exc}",
                "sandbox": sandbox_info,
            }
        parsed = parse_test_output((res.stdout or "") + (res.stderr or ""), res.exit_code, res.timed_out)
        passed = (
            not res.timed_out
            and res.exit_code == 0
            and parsed["failed"] == 0
            and parsed["errors"] == 0
            and not parsed["failed_ids"]
        )
        note = ""
        if res.timed_out:
            note = "verification timed out (sandbox killed the process tree)"
        elif not passed:
            note = "verification command failed on the integration tree"
        return {
            "passed": passed,
            "command": " ".join(command),
            "exit_code": res.exit_code,
            "failed_ids": parsed["failed_ids"][:5],
            "note": note,
            "sandbox": sandbox_info,
        }

    def cleanup(self) -> None:
        if self._path.exists():
            try:
                _git(self.repo, "worktree", "remove", "--force", str(self._path))
            except AgentError:
                shutil.rmtree(self._path, ignore_errors=True)
                _git(self.repo, "worktree", "prune")
