"""WP-V2.0-2 regression: worker runtime on the single Harness kernel.

ADR-001: a worker is the existing Harness parameterised with a RoleSpec,
budget and cancellation check — never a second loop. ADR-009: retries are new
attempts, budget exhaustion never retries, cancellation is cooperative at
step boundaries. Concurrency: different workspaces may run in parallel (bounded
by runtime.max_concurrent_workers); the same workspace is exclusive until
WP-V2.0-5 brings per-worker worktrees (v2 §6.5).
"""

import threading
import time
from pathlib import Path

import pytest

from jinlin_agent.config import load_config
from jinlin_agent.events import read_events
from jinlin_agent.ledger import TaskLedger, task_spec_from_row
from jinlin_agent.model.mock import MockProvider
from jinlin_agent.prompt import build_system_prompt
from jinlin_agent.sandbox import NullSandbox
from jinlin_agent.taskmodel import Budget, RoleSpec, TaskSpec, new_task_id
from jinlin_agent.worker_runtime import WorkerRuntime


# --------------------------------------------------------------------------- helpers


def make_ws(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text("def main():\n    return 'served'\n", encoding="utf-8")
    (root / "README.md").write_text("# demo\n\nEntry point: app.main()\n", encoding="utf-8")
    return root


def read_only_script() -> list[dict]:
    """Minimal script that reaches a legitimate (read-only) completion."""
    return [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "explain the entry point",
                            "steps": [{"description": "read README", "status": "completed", "evidence": ["README.md"]}],
                        }
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {
                        "summary": "read-only analysis finished",
                        "claims": [{"statement": "entry point is app.main", "evidence_refs": ["README.md"]}],
                    },
                }
            ]
        },
    ]


def make_runtime(tmp_path, scripts: list[list[dict]], max_concurrent_cfg: int | None = None):
    cfg = load_config(home=tmp_path / "home")
    if max_concurrent_cfg is not None:
        cfg.runtime["max_concurrent_workers"] = max_concurrent_cfg
    ledger = TaskLedger(cfg).open()
    script_iter = iter(scripts)
    provider_factory = lambda: MockProvider(next(script_iter))
    runtime = WorkerRuntime(
        cfg,
        ledger,
        provider_factory=provider_factory,
        sandbox_factory=lambda ws: NullSandbox(str(ws)),
        log=lambda msg: None,
    )
    return runtime, ledger, cfg


def make_task(ledger: TaskLedger, title="explain the entry point", budget: Budget | None = None) -> TaskSpec:
    tid = new_task_id()
    spec = TaskSpec(
        task_id=tid,
        root_task_id=tid,
        title=title,
        description="read-only analysis of the demo workspace",
        role="worker",
        capabilities=["READ"],
        budget=budget or Budget(max_tokens=300_000, max_tool_calls=200, timeout_seconds=3600, max_attempts=1),
    )
    ledger.create_task(spec)
    return spec


def wait_for_status(ledger: TaskLedger, worker_id: str, status: str, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = ledger.get_worker(worker_id)
        if row is not None and row["status"] == status:
            return True
        time.sleep(0.05)
    return False


def blocked_step_factory(hold: threading.Event):
    def blocking_step(messages):
        hold.wait(timeout=30)
        return {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]}

    return blocking_step


# --------------------------------------------------------------------------- kernel parameterisation


def test_registry_view_filters_whitelist():
    from jinlin_agent.tools.registry import build_default_registry

    registry = build_default_registry()
    full = set(registry.names())
    assert "read_file" in full and "apply_patch" in full
    view = registry.view(["read_file", "update_plan", "not_a_tool"])
    assert set(view.names()) == {"read_file", "update_plan"}
    assert view.get("apply_patch") is None


def test_role_instruction_enters_system_prompt(tmp_path):
    cfg = load_config(home=tmp_path)

    class _State:  # minimal state stub for the prompt builder
        workspace = str(tmp_path)
        sandbox_level = "L0"

    base = build_system_prompt(cfg, _State())
    with_role = build_system_prompt(cfg, _State(), role_instruction="review only; never edit")
    assert "review only; never edit" not in base
    assert "review only; never edit" in with_role


# --------------------------------------------------------------------------- lifecycle


def test_worker_runs_task_to_completed(tmp_path):
    runtime, ledger, cfg = make_runtime(tmp_path, [read_only_script()])
    spec = make_task(ledger)
    try:
        summary = runtime.run_task(spec.task_id, make_ws(tmp_path / "ws"))
        assert summary["status"] == "completed"
        assert summary["task_status"] == "completed"

        worker_row = ledger.get_worker(summary["worker_id"])
        assert worker_row["status"] == "completed"
        assert worker_row["attempt"] == 1
        assert worker_row["task_id"] == spec.task_id

        task_row = ledger.get_task(spec.task_id)
        assert task_row["status"] == "completed"
        assert task_row["result"]["status"] == "completed"
        assert "analysis" in task_row["result"]["summary"]

        # the runtime wrote its lifecycle into the task event stream
        events = [e["type"] for e in read_events(cfg.tasks_dir / spec.task_id)]
        assert "worker.registered" in events
        assert "worker.status_changed" in events
        assert "task.status_changed" in events
        assert "task.result_recorded" in events
    finally:
        runtime.shutdown()
        ledger.close()


def test_task_spec_roundtrip_from_ledger_row(tmp_path):
    runtime, ledger, _cfg = make_runtime(tmp_path, [])
    budget = Budget(max_tokens=12345, max_tool_calls=7, timeout_seconds=99, max_attempts=3)
    spec = make_task(ledger, budget=budget)
    try:
        row = ledger.get_task(spec.task_id)
        restored = task_spec_from_row(row)
        assert restored.task_id == spec.task_id
        assert restored.root_task_id == spec.task_id
        assert restored.budget.max_tokens == 12345
        assert restored.budget.max_attempts == 3
        assert restored.capabilities == ["READ"]
    finally:
        runtime.shutdown()
        ledger.close()


def test_role_whitelist_removes_tools_from_worker(tmp_path):
    restricted = RoleSpec(
        role_id="too-restricted",
        description="cannot plan",
        allowed_tools=["read_file"],
    )
    script = [
        {"tool_calls": [{"name": "update_plan", "arguments": {"plan": {"objective": "x", "steps": []}}}]},
        {"tool_calls": [{"name": "propose_completion", "arguments": {"summary": "done", "claims": []}}]},
    ]
    runtime, ledger, cfg = make_runtime(tmp_path, [script])
    spec = make_task(ledger)
    try:
        summary = runtime.run_task(spec.task_id, make_ws(tmp_path / "ws"), role_spec=restricted)
        assert summary["status"] == "failed"
        events = list(read_events(cfg.sessions_dir / summary["session_id"]))
        denied = [e for e in events if e["type"] == "tool.failed" and (e["payload"].get("error") or {}).get("code") == "TOOL_NOT_FOUND"]
        assert denied, "whitelisted-out tool must be unknown to the dispatcher"
    finally:
        runtime.shutdown()
        ledger.close()


def test_budget_max_tool_calls_fails_without_retry(tmp_path):
    runtime, ledger, _cfg = make_runtime(tmp_path, [[
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "app.py"}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {"tool_calls": [{"name": "propose_completion", "arguments": {"summary": "x", "claims": []}}]},
    ]])
    budget = Budget(max_tokens=300_000, max_tool_calls=2, timeout_seconds=3600, max_attempts=2)
    spec = make_task(ledger, budget=budget)
    try:
        summary = runtime.run_task(spec.task_id, make_ws(tmp_path / "ws"))
        # ADR-009: budget exhaustion stops the task — it never burns a retry
        assert summary["status"] == "failed"
        assert summary["error_code"] == "BUDGET_EXCEEDED"
        assert len(ledger.list_workers(spec.task_id)) == 1
        assert ledger.get_task(spec.task_id)["status"] == "failed"
        assert ledger.get_task(spec.task_id)["result"]["status"] == "failed"
    finally:
        runtime.shutdown()
        ledger.close()


def test_failed_attempt_retries_as_new_worker(tmp_path):
    fail_script = [{"raise": {"code": "MODEL_CONNECTION", "message": "flaky", "retriable": True}}]
    runtime, ledger, _cfg = make_runtime(tmp_path, [fail_script, read_only_script()])
    budget = Budget(max_tokens=300_000, max_tool_calls=200, timeout_seconds=3600, max_attempts=2)
    spec = make_task(ledger, budget=budget)
    try:
        summary = runtime.run_task(spec.task_id, make_ws(tmp_path / "ws"))
        assert summary["status"] == "completed"
        workers = ledger.list_workers(spec.task_id)
        assert [w["attempt"] for w in workers] == [1, 2]
        statuses = {w["worker_id"]: w["status"] for w in workers}
        assert statuses[workers[0]["worker_id"]] == "failed"
        assert statuses[workers[1]["worker_id"]] == "completed"
        assert ledger.get_task(spec.task_id)["status"] == "completed"
    finally:
        runtime.shutdown()
        ledger.close()


def test_cancel_stops_running_worker_cooperatively(tmp_path):
    hold = threading.Event()
    script = [blocked_step_factory(hold)] + read_only_script()[1:]
    runtime, ledger, cfg = make_runtime(tmp_path, [script])
    spec = make_task(ledger)
    try:
        worker_id = runtime.submit_task(spec.task_id, make_ws(tmp_path / "ws"))
        assert wait_for_status(ledger, worker_id, "running"), "worker never started"
        runtime.cancel_worker(worker_id, reason="owner changed their mind")
        hold.set()
        outcome = runtime.wait_worker(worker_id)
        assert outcome["status"] == "cancelled"
        assert ledger.get_task(spec.task_id)["status"] == "cancelled"
        events = list(read_events(cfg.sessions_dir / outcome["session_id"]))
        assert any(e["type"] == "session.cancelled" for e in events)
    finally:
        runtime.shutdown()
        ledger.close()


# --------------------------------------------------------------------------- concurrency


def test_same_workspace_workers_are_exclusive(tmp_path):
    hold = threading.Event()
    workspace = make_ws(tmp_path / "ws")
    runtime, ledger, _cfg = make_runtime(
        tmp_path,
        [[blocked_step_factory(hold)] + read_only_script()[1:], read_only_script()],
    )
    spec_a = make_task(ledger, title="first")
    spec_b = make_task(ledger, title="second")
    try:
        wid_a = runtime.submit_task(spec_a.task_id, workspace)
        assert wait_for_status(ledger, wid_a, "running")
        wid_b = runtime.submit_task(spec_b.task_id, workspace)
        # the same workspace is exclusive: B must stay queued while A runs
        for _ in range(20):
            if ledger.get_worker(wid_b)["status"] != "queued":
                pytest.fail("second worker on the same workspace started before the first finished")
            time.sleep(0.05)
        hold.set()
        outcome_a = runtime.wait_worker(wid_a)
        outcome_b = runtime.wait_worker(wid_b)
        assert outcome_a["status"] == "completed"
        assert outcome_b["status"] == "completed"
    finally:
        runtime.shutdown()
        ledger.close()


def test_different_workspaces_run_in_parallel(tmp_path):
    barrier = threading.Barrier(2, timeout=20)
    flags = {"parallel": False}

    def barrier_step(messages):
        try:
            barrier.wait(timeout=20)
            flags["parallel"] = True
        except threading.BrokenBarrierError:
            flags["parallel"] = False
        return {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]}

    script = [barrier_step] + read_only_script()[1:]
    runtime, ledger, _cfg = make_runtime(tmp_path, [script, script], max_concurrent_cfg=2)
    spec_a = make_task(ledger, title="left")
    spec_b = make_task(ledger, title="right")
    try:
        wid_a = runtime.submit_task(spec_a.task_id, make_ws(tmp_path / "ws_a"))
        wid_b = runtime.submit_task(spec_b.task_id, make_ws(tmp_path / "ws_b"))
        outcome_a = runtime.wait_worker(wid_a)
        outcome_b = runtime.wait_worker(wid_b)
        assert outcome_a["status"] == "completed"
        assert outcome_b["status"] == "completed"
        assert flags["parallel"], "two workers on different workspaces must be able to overlap"
    finally:
        runtime.shutdown()
        ledger.close()


def test_concurrency_limit_bounds_parallel_workers(tmp_path):
    hold = threading.Event()
    runtime, ledger, _cfg = make_runtime(
        tmp_path,
        [[blocked_step_factory(hold)] + read_only_script()[1:], read_only_script()],
        max_concurrent_cfg=1,
    )
    spec_a = make_task(ledger, title="a")
    spec_b = make_task(ledger, title="b")
    try:
        wid_a = runtime.submit_task(spec_a.task_id, make_ws(tmp_path / "ws_a"))
        assert wait_for_status(ledger, wid_a, "running")
        wid_b = runtime.submit_task(spec_b.task_id, make_ws(tmp_path / "ws_b"))
        for _ in range(20):
            if ledger.get_worker(wid_b)["status"] != "queued":
                pytest.fail("concurrency limit was not respected")
            time.sleep(0.05)
        hold.set()
        assert runtime.wait_worker(wid_a)["status"] == "completed"
        assert runtime.wait_worker(wid_b)["status"] == "completed"
    finally:
        runtime.shutdown()
        ledger.close()


# --------------------------------------------------------------------------- waiting / resume


def test_waiting_user_then_resume_completes(tmp_path):
    ask_script = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {"plan": {"objective": "wait for user", "steps": [{"description": "ask", "status": "in_progress"}]}},
                }
            ]
        },
        {"tool_calls": [{"name": "request_user_input", "arguments": {"question": "which option?"}}]},
    ]
    finish_script = [
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "wait for user",
                            "steps": [{"description": "ask", "status": "completed", "evidence": ["README.md"]}],
                        }
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {
                        "summary": "read-only analysis finished with the user answer",
                        "claims": [{"statement": "entry point is app.main", "evidence_refs": ["README.md"]}],
                    },
                }
            ]
        },
    ]
    runtime, ledger, cfg = make_runtime(tmp_path, [ask_script, finish_script])
    spec = make_task(ledger)
    try:
        worker_id = runtime.submit_task(spec.task_id, make_ws(tmp_path / "ws"))
        outcome = runtime.wait_worker(worker_id)
        assert outcome["status"] == "waiting_user"
        assert ledger.get_worker(worker_id)["status"] == "waiting_user"
        assert ledger.get_task(spec.task_id)["status"] == "waiting_user"

        resumed = runtime.resume_worker(worker_id, answer="use option X")
        assert resumed["status"] == "completed"
        assert ledger.get_task(spec.task_id)["status"] == "completed"
        session_events = list(read_events(cfg.sessions_dir / outcome["session_id"]))
        assert any(
            e["type"] == "session.user_input" and "option X" in str((e.get("payload") or {}).get("text", ""))
            for e in session_events
        )
    finally:
        runtime.shutdown()
        ledger.close()
