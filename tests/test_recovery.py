"""WP-R5/R6/R7 regression: recovery and consistency after restart.

Review task book §四: process restarts must not silently lose worktree
mappings, mark interrupted work as success, or complete a root whose
deliverables were never integrated. A completed task with changes but no
integration commit BLOCKS the root (adjudication), never pretends.
"""

import json

import pytest
import sqlite3
from pathlib import Path

from jinlin_agent.config import load_config
from jinlin_agent.ledger import TaskLedger
from jinlin_agent.model.mock import MockProvider
from jinlin_agent.sandbox import NullSandbox
from jinlin_agent.supervisor import Supervisor
from jinlin_agent.taskmodel import Budget, TaskSpec, WorkerRecord, new_task_id, new_worker_id
from jinlin_agent.worker_runtime import WorkerRuntime, recover_interrupted
from jinlin_agent.workspace_manager import WorkspaceManager

from test_worktree import make_git_repo, plan_step, assess_step, sub


# --------------------------------------------------------------------------- worktree recovery


def test_worktree_recovery_adopts_live_and_reports_orphans(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    manager1 = WorkspaceManager(repo, cfg, ledger)
    root_id = new_task_id()
    tid = new_task_id()
    spec = TaskSpec(task_id=tid, root_task_id=root_id, title="live task", capabilities=["READ", "WRITE"])
    ledger.create_task(spec)
    worker = WorkerRecord(
        worker_id=new_worker_id(), task_id=tid, root_task_id=root_id,
        attempt=1, status="running", workspace_path="", branch="agent/task-" + tid,
        base_commit="",  # recovered from git if unknown
    )
    ledger.register_worker(worker)
    ws = manager1.create_worker_workspace(tid)
    # an unregistered worktree directory planted on disk = orphan candidate
    orphan_dir = manager1.home / "task_deadbeef"
    orphan_dir.mkdir(parents=True, exist_ok=True)
    # restart simulation: manager2 is a fresh instance with an empty map;
    # the on-disk worktree stays (we do NOT call manager1.cleanup_*)
    manager2 = WorkspaceManager(repo, cfg, ledger)
    report = manager2.recover(ledger, root_id)
    try:
        assert [w.task_id for w in report["adopted"]] == [tid]
        adopted = manager2.get(tid)
        assert adopted is not None and adopted.branch == "agent/task-" + tid
        assert adopted.path.exists()
        assert report["orphans"] and report["orphans"][0]["path"].endswith("task_deadbeef")
    finally:
        manager2.cleanup_all()
        for orphan in report["orphans"]:
            pass  # orphans are reported, never auto-deleted (WP-R5.7)
        ledger.close()


def test_recover_read_worker_base_commit_from_the_ledger(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    manager1 = WorkspaceManager(repo, cfg, ledger)
    root_id = new_task_id()
    tid = new_task_id()
    spec = TaskSpec(task_id=tid, root_task_id=root_id, title="t", capabilities=["READ", "WRITE"])
    ledger.create_task(spec)
    base = __import__("subprocess").run(
        ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True
    ).stdout.strip()
    worker = WorkerRecord(
        worker_id=new_worker_id(), task_id=tid, root_task_id=root_id,
        attempt=1, status="running", branch="agent/task-" + tid, base_commit=base,
    )
    ledger.register_worker(worker)
    manager1.create_worker_workspace(tid, base_commit=base)
    manager1._worktrees.clear()  # lose the in-memory map (restart simulation)

    manager2 = WorkspaceManager(repo, cfg, ledger)
    manager2.recover(ledger, root_id)
    try:
        adopted = manager2.get(tid)
        assert adopted.base_commit == base, "base_commit must come from the persisted record"
    finally:
        manager2.cleanup_all()
        ledger.close()


# --------------------------------------------------------------------------- worker recovery


def make_runtime(tmp_path, scripts):
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    worker_iter = iter(scripts)
    runtime = WorkerRuntime(
        cfg, ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda w: NullSandbox(str(w)),
        log=lambda m: None,
    )
    return cfg, ledger, runtime


def test_interrupted_running_worker_fails_closed_and_blocks_the_task(tmp_path):
    cfg, ledger, runtime = make_runtime(tmp_path, [])
    root_id = new_task_id()
    tid = new_task_id()
    spec = TaskSpec(task_id=tid, root_task_id=root_id, title="was running", capabilities=["READ"])
    ledger.create_task(spec)
    ledger.set_task_status(tid, "running")
    # fabricate a crashed attempt: worker row says running, session exists
    worker = WorkerRecord(
        worker_id=new_worker_id(), task_id=tid, root_task_id=root_id,
        attempt=1, status="running", session_id="sess_missing",
    )
    ledger.register_worker(worker)
    try:
        report = recover_interrupted(cfg, ledger, log=lambda m: None)
        assert report["failed_recovered"] == 1
        assert ledger.get_worker(worker.worker_id)["status"] == "failed"
        # running -> blocked: needs adjudication, never an auto-retry success
        assert ledger.get_task(tid)["status"] == "blocked"
    finally:
        runtime.shutdown()
        ledger.close()


def test_completed_session_reconciles_the_worker_row(tmp_path):
    """A worker that finished but whose row never updated (crash after
    session.completed) is reconciled from the session truth."""
    cfg, ledger, runtime = make_runtime(tmp_path, [])
    root_id = new_task_id()
    tid = new_task_id()
    spec = TaskSpec(task_id=tid, root_task_id=root_id, title="done", capabilities=["READ"])
    ledger.create_task(spec)
    ledger.set_task_status(tid, "running")
    sandbox = NullSandbox(str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "README.md").write_text("# demo\n", encoding="utf-8")
    from jinlin_agent.session import SessionManager

    session = SessionManager.create(cfg, tmp_path / "ws", "completed off-ledger", sandbox)
    session.emit(
        "session.completed",
        {"result": "completed", "summary": "analysis done"},
    )
    session.close()
    worker = WorkerRecord(
        worker_id=new_worker_id(), task_id=tid, root_task_id=root_id,
        attempt=1, status="running", session_id=session.id,
    )
    ledger.register_worker(worker)
    try:
        report = recover_interrupted(cfg, ledger, log=lambda m: None)
        assert report["completed_reconciled"] == 1
        assert ledger.get_worker(worker.worker_id)["status"] == "completed"
        assert ledger.get_task(tid)["status"] == "completed"
    finally:
        runtime.shutdown()
        ledger.close()


def test_queued_workers_survive_restart_reschedulable(tmp_path):
    cfg, ledger, runtime = make_runtime(tmp_path, [])
    root_id = new_task_id()
    tid = new_task_id()
    spec = TaskSpec(task_id=tid, root_task_id=root_id, title="queued", capabilities=["READ"])
    ledger.create_task(spec)
    worker = WorkerRecord(
        worker_id=new_worker_id(), task_id=tid, root_task_id=root_id,
        attempt=1, status="queued",
    )
    ledger.register_worker(worker)
    try:
        report = recover_interrupted(cfg, ledger, log=lambda m: None)
        assert report["failed_recovered"] == 0
        assert ledger.get_worker(worker.worker_id)["status"] == "queued"  # reschedulable
    finally:
        runtime.shutdown()
        ledger.close()


# --------------------------------------------------------------------------- supervisor integration recovery


def _stack(tmp_path, worker_scripts, supervisor_script, repo):
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    worker_iter = iter(worker_scripts)
    runtime = WorkerRuntime(
        cfg, ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda w: NullSandbox(str(w)),
        log=lambda m: None,
    )
    supervisor = Supervisor(cfg, ledger, runtime, provider=MockProvider(supervisor_script) if supervisor_script else None, log=lambda m: None)
    manager = WorkspaceManager(repo, cfg, ledger)
    supervisor.attach_workspace_manager(manager)
    return cfg, ledger, runtime, supervisor, manager


def _read_only_finish():
    return [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {"plan": {"objective": "x", "steps": [{"description": "read", "status": "completed", "evidence": ["README.md"]}]}},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {"summary": "read-only analysis finished", "claims": [{"statement": "entry is app.main", "evidence_refs": ["README.md"]}]},
                }
            ]
        },
    ]


def test_restart_with_adopted_worktrees_still_integrates(tmp_path):
    repo = make_git_repo(tmp_path / "repo")
    (repo / "README.md").write_text("# calc", encoding="utf-8")
    import subprocess as _sp

    _sp.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "add", "-A"], cwd=str(repo), capture_output=True)
    _sp.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-qm", "readme"], cwd=str(repo), capture_output=True)
    plan = plan_step([sub("write thing")])
    cfg, ledger, runtime, supervisor, manager = _stack(
        tmp_path, [_read_only_finish()], [assess_step(), plan], repo,
    )
    tid = new_task_id()
    root = TaskSpec(task_id=tid, root_task_id=tid, title="1) write thing", capabilities=["READ", "WRITE"])
    ledger.create_task(root)
    try:
        # run the worker to completion in a worktree, then simulate a restart:
        # the supervisor's in-memory worktree map is lost
        sub_spec = TaskSpec(
            task_id=new_task_id(), root_task_id=tid, parent_task_id=tid,
            title="write thing", capabilities=["READ", "WRITE"],
            budget=Budget(max_tokens=300_000, max_tool_calls=200, timeout_seconds=3600, max_attempts=1),
        )
        ledger.create_task(sub_spec)
        ledger.set_task_status(sub_spec.task_id, "queued")
        ledger.set_task_status(tid, "running", reason="planning")
        ws = manager.create_worker_workspace(sub_spec.task_id)
        wid = runtime.submit_task(sub_spec.task_id, ws.path, branch=ws.branch, base_commit=ws.base_commit)
        outcome = runtime.wait_worker(wid)
        assert outcome["status"] == "completed", outcome
        manager._worktrees.clear()  # RESTART: in-memory map lost

        report = manager.recover(ledger, tid)
        assert report["adopted"], "restart must re-adopt the persisted worktree"
        integration = supervisor._integrate_all(tid)
        assert integration["status"] == "integrated", integration
        row = ledger.get_task(sub_spec.task_id)
        assert row["integration"] == "integrated"
        assert row["result"]["commit"], "the recovered integration must bind a commit"
    finally:
        manager.cleanup_all()
        runtime.shutdown()
        ledger.close()


def test_completed_task_without_integration_blocks_the_root(tmp_path):
    """WP-R6: a completed task with changes but no integration commit must
    NOT let the root complete silently (restart lost the worktree map)."""
    repo = make_git_repo(tmp_path / "repo")
    cfg, ledger, runtime, supervisor, manager = _stack(tmp_path, [], [], repo)
    tid = new_task_id()
    root = TaskSpec(task_id=tid, root_task_id=tid, title="1) write thing", capabilities=["READ", "WRITE"])
    ledger.create_task(root)
    sub_spec = TaskSpec(
        task_id=new_task_id(), root_task_id=tid, parent_task_id=tid,
        title="write thing", capabilities=["READ", "WRITE"],
        budget=Budget(max_tokens=300_000, max_tool_calls=200, timeout_seconds=3600, max_attempts=1),
    )
    ledger.create_task(sub_spec)
    # fabricate post-restart state: task completed, result has changes, NO commit
    ledger.set_task_status(sub_spec.task_id, "running", reason="pre-restart")
    ledger.set_task_status(sub_spec.task_id, "completed", reason="pre-restart")
    ledger.record_result(
        sub_spec.task_id,
        {"status": "completed", "summary": "work done", "changed_files": ["calc.py"], "tests": []},
    )
    ledger.set_task_status(tid, "running")
    try:
        outcome = supervisor.execute_root(root, repo)
        assert outcome["status"] == "blocked", outcome
        assert "integration" in json.dumps(outcome).lower()
        assert ledger.get_task(tid)["status"] == "blocked"
    finally:
        runtime.shutdown()
        ledger.close()


# --------------------------------------------------------------------------- schema v3


def test_schema_v3_migration_adds_recovery_columns(tmp_path):
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    ledger.close()
    conn = sqlite3.connect(str(cfg.ledger_path))
    conn.execute("ALTER TABLE workers DROP COLUMN last_commit")
    conn.execute("ALTER TABLE tasks DROP COLUMN integration")
    conn.execute("PRAGMA user_version = 2")
    conn.commit()
    conn.close()

    ledger2 = TaskLedger(cfg).open()
    assert ledger2.SCHEMA_VERSION == 3
    row = ledger2.summary()
    assert row is not None
    ledger2.close()


# --------------------------------------------------------------------------- WP-R7: approval vs user-input


def _fabricate_waiting(ledger, root_id=None, status="waiting_approval"):
    root_id = root_id or new_task_id()
    tid = new_task_id()
    spec = TaskSpec(task_id=tid, root_task_id=root_id, title="waiting", capabilities=["READ"])
    ledger.create_task(spec)
    ledger.set_task_status(tid, "running")
    ledger.set_task_status(tid, status, reason="waiting")
    worker = WorkerRecord(
        worker_id=new_worker_id(), task_id=tid, root_task_id=root_id,
        attempt=1, status=status, session_id="sess_fabricated",
    )
    ledger.register_worker(worker)
    return spec, worker


def test_waiting_approval_cannot_resume_with_free_text(tmp_path):
    cfg, ledger, runtime = make_runtime(tmp_path, [])
    _spec, worker = _fabricate_waiting(ledger, status="waiting_approval")
    try:
        with pytest.raises(Exception) as exc:
            runtime.resume_worker(worker.worker_id, answer="just trust me")
        assert "approval_decision" in str(exc.value)
        # the worker row is untouched: no permission was granted by text
        assert ledger.get_worker(worker.worker_id)["status"] == "waiting_approval"
    finally:
        runtime.shutdown()
        ledger.close()


def test_waiting_approval_worker_grant_is_refused(tmp_path):
    """approved_once through the headless worker path is a policy bypass and
    is refused honestly (interactive CLI approval is the only grant path)."""
    cfg, ledger, runtime = make_runtime(tmp_path, [])
    _spec, worker = _fabricate_waiting(ledger, status="waiting_approval")
    try:
        with pytest.raises(Exception) as exc:
            runtime.resume_worker(worker.worker_id, approval_decision="approved_once")
        assert "approval" in str(exc.value).lower()
        assert ledger.get_worker(worker.worker_id)["status"] == "waiting_approval"
    finally:
        runtime.shutdown()
        ledger.close()


def test_waiting_approval_deny_decision_resumes(tmp_path):
    cfg, ledger, runtime = make_runtime(tmp_path, [])
    _spec, worker = _fabricate_waiting(ledger, status="waiting_approval")
    try:
        # the DENY decision must pass the approval gate; the fabricated
        # session then fails at open (crash handling), NOT at the approval gate
        outcome = runtime.resume_worker(worker.worker_id, approval_decision="denied")
        row = ledger.get_worker(worker.worker_id)
        assert row["status"] == "failed"  # proceeded past the approval gate
        assert "session" in outcome.get("summary", "").lower() or "crash" in outcome.get("summary", "").lower()
    finally:
        runtime.shutdown()
        ledger.close()
