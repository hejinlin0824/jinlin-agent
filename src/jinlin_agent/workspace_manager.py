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
        self.home = Path(config.home) / "worktrees"
        self._worktrees: dict[str, WorkerWorkspace] = {}
        self._coordinators: dict[str, IntegrationCoordinator] = {}

    @staticmethod
    def is_git_repo(path: Path | str) -> bool:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(path),
            capture_output=True,
            text=True,
            timeout=30,
        )
        return res.returncode == 0 and res.stdout.strip() == "true"

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


class IntegrationCoordinator:
    def __init__(self, repo: Path | str, config: Any, ledger: TaskLedger | None, root_id: str, manager: WorkspaceManager) -> None:
        self.repo = Path(repo).resolve()
        self.config = config
        self.ledger = ledger
        self.root_id = root_id
        self.manager = manager
        self.integration_branch = f"agent/integration-{root_id}"
        self._path = Path(config.home) / "worktrees" / f"integration-{root_id}"
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
        """Run the project's own tests on the integration tree (v2: merged
        changes are re-verified; 'affected only' targeting arrives with
        WP-V2.0-8's diff-aware planning)."""
        project = detect_project(self._path, self.config)
        if not project.test_commands:
            return {"passed": True, "command": "", "exit_code": None, "note": "no test command detected"}
        command = project.test_commands[0]
        try:
            res = subprocess.run(
                command,
                cwd=str(self._path),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(self.config.runtime.get("max_shell_timeout_seconds", 1800)),
                env=process_env(str(self._path)),
            )
        except subprocess.TimeoutExpired:
            return {"passed": False, "command": " ".join(command), "exit_code": None, "note": "verification timed out"}
        parsed = parse_test_output((res.stdout or "") + (res.stderr or ""), res.returncode, False)
        passed = res.returncode == 0 and parsed["failed"] == 0 and parsed["errors"] == 0 and not parsed["failed_ids"]
        return {
            "passed": passed,
            "command": " ".join(command),
            "exit_code": res.returncode,
            "failed_ids": parsed["failed_ids"][:5],
            "note": "",
        }

    def cleanup(self) -> None:
        if self._path.exists():
            try:
                _git(self.repo, "worktree", "remove", "--force", str(self._path))
            except AgentError:
                shutil.rmtree(self._path, ignore_errors=True)
                _git(self.repo, "worktree", "prune")
