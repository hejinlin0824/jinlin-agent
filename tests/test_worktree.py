"""WP-V2.0-5 regression: git worktree isolation and the integration coordinator.

ADR-005: every worker works in its own worktree on agent/task-<id>; the worktrees
live under the harness home (never inside the user's repo, never outside the
worker's workspace boundary). Integration merges task branches into
agent/integration-<root>, rebases stale bases, PAUSES on conflict
(integration.conflict, nothing silently overwritten), runs verification on the
integration tree after each merge, and rolls the merge back when it fails.
The stable branch is never touched before the root completion gate.
"""

import json
import subprocess
from pathlib import Path

import pytest

from jinlin_agent.config import load_config
from jinlin_agent.ledger import TaskLedger
from jinlin_agent.model.mock import MockProvider
from jinlin_agent.sandbox import NullSandbox
from jinlin_agent.supervisor import Supervisor
from jinlin_agent.taskmodel import Budget, TaskSpec, new_task_id
from jinlin_agent.worker_runtime import WorkerRuntime
from jinlin_agent.workspace_manager import IntegrationCoordinator, WorkspaceManager


# --------------------------------------------------------------------------- fixtures


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(
        ["git", "-c", "user.email=agent@example.com", "-c", "user.name=agent", *[str(a) for a in args]],
        cwd=str(repo),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert res.returncode == 0, f"git {args} failed: {res.stderr}"
    return (res.stdout or "").strip()


def make_git_repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (root / "tests").mkdir(exist_ok=True)
    (root / "tests" / "test_calc.py").write_text("from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
    (root / "conftest.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text('[project]\nname = "calc"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8")
    (root / ".jinlin").mkdir()
    (root / ".jinlin" / "config.toml").write_text('[verification]\ntest_command = ["python", "-m", "pytest", "-q"]\n', encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n.pytest_cache/\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def edit_script(path: str, old: str, new: str) -> list[dict]:
    return [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": path}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {"plan": {"objective": "edit", "steps": [{"description": "edit", "status": "completed", "evidence": [path]}]}},
                }
            ]
        },
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": path, "old_string": old, "new_string": new}}]},
        {"tool_calls": [{"name": "propose_completion", "arguments": {"summary": f"read-only analysis finished: {path} edited", "claims": [{"statement": f"{path} updated", "evidence_refs": [path]}]}}]},
    ]


def make_stack(tmp_path, worker_scripts, supervisor_script, git_repo: Path | None = None):
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    worker_iter = iter(worker_scripts)
    runtime = WorkerRuntime(
        cfg,
        ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda ws: NullSandbox(str(ws)),
        log=lambda msg: None,
    )
    from jinlin_agent.supervisor import Supervisor as _S

    supervisor = Supervisor(cfg, ledger, runtime, provider=MockProvider(supervisor_script) if supervisor_script else None, log=lambda msg: None)
    manager = None
    if git_repo is not None:
        manager = WorkspaceManager(git_repo, cfg, ledger)
        supervisor.attach_workspace_manager(manager)
    return cfg, ledger, runtime, supervisor, manager


def plan_step(subtasks):
    return {"text": "```json\n" + json.dumps({"subtasks": subtasks}) + "\n```"}


def assess_step(strategy="plan_then_execute"):
    return {"text": json.dumps({"strategy": strategy, "reasons": ["scripted"]})}


def sub(title, depends_on=None, capabilities=None):
    return {
        "title": title,
        "description": f"do {title}",
        "depends_on": depends_on or [],
        "role": "worker",
        "capabilities": capabilities or ["READ", "WRITE"],
        "acceptance_criteria": [f"{title} done"],
    }


# --------------------------------------------------------------------------- workspace manager


def test_is_git_repo_requires_a_repo_root_not_a_subdirectory(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    assert WorkspaceManager.is_git_repo(repo)
    # a directory INSIDE a repo is not a repo the manager may attach to:
    # branches and worktrees would land on the outer checkout
    assert not WorkspaceManager.is_git_repo(repo / "tests")
    assert not WorkspaceManager.is_git_repo(tmp_path / "plain")


def test_create_worker_worktree_records_branch_and_base(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    manager = WorkspaceManager(repo, cfg, ledger)
    base = _git(repo, "rev-parse", "HEAD")
    ws = manager.create_worker_workspace("task_abc")
    try:
        assert ws.task_id == "task_abc"
        assert ws.branch == "agent/task-task_abc"
        assert ws.base_commit == base
        assert (ws.path / "calc.py").exists()  # full checkout
        listed = _git(repo, "worktree", "list")
        assert "agent/task-task_abc" in listed
    finally:
        manager.cleanup_worker_workspace("task_abc")
        ledger.close()
    assert not ws.path.exists()  # worktree removed
    assert _git(repo, "rev-parse", "--verify", "agent/task-task_abc")  # branch kept as evidence


def test_cleanup_is_idempotent(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    manager = WorkspaceManager(repo, cfg, ledger)
    ws = manager.create_worker_workspace("task_xyz")
    manager.cleanup_worker_workspace("task_xyz")
    manager.cleanup_worker_workspace("task_xyz")  # no error twice
    manager.cleanup_worker_workspace("task_never_created")  # no error
    ledger.close()


# --------------------------------------------------------------------------- integration coordinator


def test_integrate_commits_and_merges_with_verification(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    root_id = new_task_id()
    manager = WorkspaceManager(repo, cfg, ledger)
    ws = manager.create_worker_workspace("task_t1")
    coordinator = IntegrationCoordinator(repo, cfg, ledger, root_id=root_id, manager=manager)
    try:
        # the worker edits inside its worktree (non-breaking comment)
        code = (ws.path / "calc.py").read_text(encoding="utf-8")
        (ws.path / "calc.py").write_text(code.replace("a + b", "a + b  # summed"), encoding="utf-8")
        outcome = coordinator.integrate("task_t1", ws)
        assert outcome["status"] == "integrated", outcome
        assert outcome["commit"]
        integration_path = coordinator.integration_path()
        assert "a + b" in (integration_path / "calc.py").read_text(encoding="utf-8")
        # verification ran on the integration tree and passed
        events = [e["type"] for e in __import__("jinlin_agent.events", fromlist=["read_events"]).read_events(cfg.tasks_dir / root_id)]
        assert "integration.verified" in events
        assert outcome["verification"]["passed"] is True
    finally:
        manager.cleanup_all()
        ledger.close()


def test_conflict_pauses_integration_without_overwriting(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    root_id = new_task_id()
    manager = WorkspaceManager(repo, cfg, ledger)
    coordinator = IntegrationCoordinator(repo, cfg, ledger, root_id=root_id, manager=manager)
    ws_a = manager.create_worker_workspace("task_a")
    ws_b = manager.create_worker_workspace("task_b")
    try:
        for ws in (ws_a, ws_b):
            code = (ws.path / "calc.py").read_text(encoding="utf-8")
            (ws.path / "calc.py").write_text(code.replace("a + b", f"a + b  # {'A' if ws is ws_a else 'B'}"), encoding="utf-8")
        first = coordinator.integrate("task_a", ws_a)
        assert first["status"] == "integrated"
        second = coordinator.integrate("task_b", ws_b)
        assert second["status"] == "conflict"
        assert second["conflict_files"], "conflicting files must be named"
        # the integration tree is untouched by the conflicting merge (A's version wins for now)
        current = (coordinator.integration_path() / "calc.py").read_text(encoding="utf-8")
        assert "# A" in current and "# B" not in current
        # the conflict was recorded as a protocol message for adjudication
        messages = [m["message_type"] for m in ledger.list_messages(root_id)]
        assert "integration.conflict" in messages
    finally:
        manager.cleanup_all()
        ledger.close()


def test_stale_worker_base_rebases_onto_integration(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    root_id = new_task_id()
    manager = WorkspaceManager(repo, cfg, ledger)
    coordinator = IntegrationCoordinator(repo, cfg, ledger, root_id=root_id, manager=manager)
    ws_a = manager.create_worker_workspace("task_a")  # both created from the same base
    ws_b = manager.create_worker_workspace("task_b")
    try:
        (ws_a.path / "calc.py").write_text("def add(a, b):\n    \"\"\"Sum two numbers.\"\"\"\n    return a + b\n", encoding="utf-8")
        (ws_b.path / "docs.md").write_text("worker B notes\n", encoding="utf-8")
        assert coordinator.integrate("task_a", ws_a)["status"] == "integrated"
        # B's base is now behind integration: rebase path, non-conflicting
        outcome = coordinator.integrate("task_b", ws_b)
        assert outcome["status"] == "integrated", outcome
        assert outcome.get("rebased") is True
        integration_path = coordinator.integration_path()
        assert '"""Sum two numbers."""' in (integration_path / "calc.py").read_text(encoding="utf-8")
        assert (integration_path / "docs.md").exists()
    finally:
        manager.cleanup_all()
        ledger.close()


def test_failed_verification_rolls_back_the_merge(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    root_id = new_task_id()
    manager = WorkspaceManager(repo, cfg, ledger)
    coordinator = IntegrationCoordinator(repo, cfg, ledger, root_id=root_id, manager=manager)
    ws = manager.create_worker_workspace("task_bad")
    try:
        # a change that breaks the suite on the integration tree
        code = (ws.path / "calc.py").read_text(encoding="utf-8")
        (ws.path / "calc.py").write_text(code.replace("return a + b", "return a + b + 1"), encoding="utf-8")
        outcome = coordinator.integrate("task_bad", ws)
        assert outcome["status"] == "verification_failed", outcome
        # the merge was rolled back: the broken code is NOT on the integration tree
        current = (coordinator.integration_path() / "calc.py").read_text(encoding="utf-8")
        assert "a + b + 1" not in current
    finally:
        manager.cleanup_all()
        ledger.close()


# --------------------------------------------------------------------------- supervisor end to end


def test_supervisor_runs_workers_in_worktrees_and_integrates(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    supervisor_script = [
        assess_step(),
        plan_step([sub("fix add"), sub("add docs")]),
    ]
    cfg, ledger, runtime, supervisor, manager = make_stack(
        tmp_path,
        [edit_script("calc.py", "return a + b", "return a + b  # reviewed"), edit_script("README.md", "# calc", "# calc (edited)")],
        supervisor_script,
        git_repo=repo,
    )
    (repo / "README.md").write_text("# calc\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "readme")
    tid = new_task_id()
    root = TaskSpec(task_id=tid, root_task_id=tid, title="1) fix add\n2) add docs", capabilities=["READ", "WRITE"])
    ledger.create_task(root)
    try:
        outcome = supervisor.execute_root(root, repo)
        assert outcome["status"] == "completed", outcome
        # both worker changes are on the integration branch
        integration_path = manager.integration_path()
        assert "a + b  # reviewed" in (integration_path / "calc.py").read_text(encoding="utf-8")
        assert "# calc (edited)" in (integration_path / "README.md").read_text(encoding="utf-8")
        # commits are attached to each task result; worker worktrees cleaned up
        for task_row in ledger.list_tasks(tid):
            if task_row["task_id"] != tid:
                assert task_row["result"]["commit"], f"task {task_row['title']} has no attached commit"
        assert not list((repo / ".jinlin" / "worktrees").glob("task_*"))
        assert (repo / ".jinlin" / "worktrees" / f"integration-{tid}").exists()  # deliverable staging kept
        # the STABLE branch was never touched before the root gate
        stable = (repo / "calc.py").read_text(encoding="utf-8")
        assert "# reviewed" not in stable  # stable branch still has the original code
    finally:
        if manager is not None:
            manager.cleanup_all()
        runtime.shutdown()
        ledger.close()
