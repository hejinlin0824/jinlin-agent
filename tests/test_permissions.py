"""WP-V2.0-6 regression: capability sets and workspace scopes enforced in code.

ADR-006: permissions are enforced by the PolicyEngine, never by prompt. A
task's capability set (READ/WRITE/EXECUTE/NETWORK/DELETE) and its
workspace_scope path ranges gate every side effect; unrestricted behaviour
(capabilities=None, empty scope) is preserved for the single-agent path.
Prompt content cannot grant anything — these checks run before approval and
before tool.intent.
"""

from pathlib import Path

import pytest

from jinlin_agent.config import load_config
from jinlin_agent.errors import CAPABILITY_DENIED, SCOPE_VIOLATED
from jinlin_agent.policy import PolicyEngine
from jinlin_agent.taskmodel import WorkspaceScope


def make_policy(tmp_path, capabilities=None, scope=None, read_only=False, auto_approve=False) -> PolicyEngine:
    cfg = load_config(home=tmp_path / "home")
    (tmp_path / "ws" / "docs").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ws" / "src").mkdir(parents=True, exist_ok=True)
    return PolicyEngine(
        cfg,
        tmp_path / "ws",
        approval_handler=None,
        auto_approve=auto_approve,
        read_only=read_only,
        task_capabilities=capabilities,
        workspace_scope=scope,
    )


# --------------------------------------------------------------------------- capability set


def test_write_tool_requires_write_capability(tmp_path):
    without = make_policy(tmp_path, capabilities={"READ"})
    decision = without.evaluate("edit_file", "WRITE", {"path": "src/x.py"})
    assert not decision.allowed
    assert decision.error.code == CAPABILITY_DENIED

    with_cap = make_policy(tmp_path, capabilities={"READ", "WRITE"})
    assert with_cap.evaluate("edit_file", "WRITE", {"path": "src/x.py"}).allowed


def test_execute_tool_requires_execute_capability(tmp_path):
    without = make_policy(tmp_path, capabilities={"READ", "WRITE"})
    decision = without.evaluate("run_command", "EXECUTE", {"command": ["python", "-m", "pytest", "-q"], "cwd": "."})
    assert not decision.allowed
    assert decision.error.code == CAPABILITY_DENIED

    with_cap = make_policy(tmp_path, capabilities={"READ", "WRITE", "EXECUTE"})
    assert with_cap.evaluate("run_command", "EXECUTE", {"command": ["python", "-m", "pytest", "-q"], "cwd": "."}).allowed


def test_network_command_requires_network_capability_even_with_execute(tmp_path):
    execute_only = make_policy(tmp_path, capabilities={"READ", "WRITE", "EXECUTE"})
    decision = execute_only.evaluate("run_command", "EXECUTE", {"command": ["curl", "https://example.com"], "cwd": "."})
    assert not decision.allowed
    assert decision.error.code == CAPABILITY_DENIED

    with_network = make_policy(tmp_path, capabilities={"READ", "EXECUTE", "NETWORK"}, auto_approve=True)
    assert with_network.evaluate("run_command", "EXECUTE", {"command": ["curl", "https://example.com"], "cwd": "."}).allowed


def test_destructive_command_requires_delete_capability(tmp_path):
    without = make_policy(tmp_path, capabilities={"READ", "WRITE", "EXECUTE"})
    decision = without.evaluate("run_command", "EXECUTE", {"command": ["git", "reset", "--hard"], "cwd": "."})
    assert not decision.allowed
    assert decision.error.code == CAPABILITY_DENIED

    with_delete = make_policy(tmp_path, capabilities={"READ", "EXECUTE", "DELETE"}, auto_approve=True)
    # isolate the capability layer: the approval default (deny) is a separate gate
    with_delete.config.approval["shell_destructive"] = "auto"
    assert with_delete.evaluate("run_command", "EXECUTE", {"command": ["git", "reset", "--hard"], "cwd": "."}).allowed


def test_read_tools_stay_available_under_narrow_capability(tmp_path):
    narrow = make_policy(tmp_path, capabilities={"WRITE"})
    assert narrow.evaluate("read_file", "READ", {"path": "src/x.py"}).allowed
    assert narrow.evaluate("update_plan", "NONE", {}).allowed


def test_unrestricted_when_no_capability_set(tmp_path):
    legacy = make_policy(tmp_path, capabilities=None)
    assert legacy.evaluate("edit_file", "WRITE", {"path": "src/x.py"}).allowed
    assert legacy.evaluate("run_command", "EXECUTE", {"command": ["python", "-m", "pytest", "-q"], "cwd": "."}).allowed


# --------------------------------------------------------------------------- workspace scope


def test_write_outside_scope_is_rejected(tmp_path):
    scoped = make_policy(tmp_path, capabilities={"READ", "WRITE"}, scope=WorkspaceScope(read=["."], write=["docs/"]))
    decision = scoped.evaluate("edit_file", "WRITE", {"path": "src/x.py"})
    assert not decision.allowed
    assert decision.error.code == SCOPE_VIOLATED

    inside = scoped.evaluate("edit_file", "WRITE", {"path": "docs/x.md"})
    assert inside.allowed


def test_patch_paths_are_scope_checked(tmp_path):
    scoped = make_policy(tmp_path, capabilities={"READ", "WRITE"}, scope=WorkspaceScope(read=["."], write=["docs/"]))
    patch = "*** Begin Patch\n*** Update File: src/escape.py\n@@\n-x\n+y\n*** End Patch"
    decision = scoped.evaluate("apply_patch", "WRITE", {"patch": patch})
    assert not decision.allowed
    assert decision.error.code == SCOPE_VIOLATED

    ok_patch = "*** Begin Patch\n*** Update File: docs/ok.md\n@@\n-x\n+y\n*** End Patch"
    assert scoped.evaluate("apply_patch", "WRITE", {"patch": ok_patch}).allowed


def test_read_scope_gates_read_tools_when_declared(tmp_path):
    scoped = make_policy(tmp_path, capabilities={"READ"}, scope=WorkspaceScope(read=["docs/"], write=[]))
    assert scoped.evaluate("read_file", "READ", {"path": "docs/x.md"}).allowed
    decision = scoped.evaluate("read_file", "READ", {"path": "src/secret.py"})
    assert not decision.allowed
    assert decision.error.code == SCOPE_VIOLATED


def test_empty_scope_means_unrestricted(tmp_path):
    unscoped = make_policy(tmp_path, capabilities={"READ", "WRITE"}, scope=WorkspaceScope(read=[], write=[]))
    assert unscoped.evaluate("edit_file", "WRITE", {"path": "anything/anywhere.py"}).allowed


# --------------------------------------------------------------------------- integration: worker inherits the contract


def test_worker_runtime_enforces_the_task_capability_set(tmp_path):
    from jinlin_agent.ledger import TaskLedger
    from jinlin_agent.model.mock import MockProvider
    from jinlin_agent.sandbox import NullSandbox
    from jinlin_agent.taskmodel import Budget, TaskSpec, new_task_id
    from jinlin_agent.worker_runtime import WorkerRuntime
    from jinlin_agent.events import read_events

    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    ws = tmp_path / "ws"
    (ws / "docs").mkdir(parents=True, exist_ok=True)
    script = [
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "app.py", "old_string": "def main():", "new_string": "def main():  # x"}}]},
    ]
    worker_iter = iter([script])
    runtime = WorkerRuntime(
        cfg,
        ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda w: NullSandbox(str(w)),
        log=lambda msg: None,
    )
    tid = new_task_id()
    spec = TaskSpec(
        task_id=tid,
        root_task_id=tid,
        title="read-only task tries to edit",
        capabilities=["READ"],
        budget=Budget(max_tokens=300_000, max_tool_calls=200, timeout_seconds=3600, max_attempts=1),
    )
    ledger.create_task(spec)
    try:
        worker_id = runtime.submit_task(tid, ws)
        outcome = runtime.wait_worker(worker_id)
        assert outcome["status"] == "failed"
        session_dir = cfg.sessions_dir / outcome["session_id"]
        events = list(read_events(session_dir))
        denied = [e for e in events if e["type"] == "tool.denied"]
        assert denied, "capability denial must surface as tool.denied"
        assert (denied[0]["payload"].get("error") or {}).get("code") == CAPABILITY_DENIED
    finally:
        runtime.shutdown()
        ledger.close()
