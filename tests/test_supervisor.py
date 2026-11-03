"""WP-V2.0-3 regression: dynamic planning, Supervisor, DAG scheduling, bounded replanning.

ADR-002: direct execution is the default (simple tasks never plan and never
create extra workers). ADR-009: replanning is bounded (runtime.max_replans);
a task whose upstream was replaced cancels its non-terminal downstream (stale)
instead of silently reusing them. The planner is model-driven with a validated
JSON schema; a deterministic assessment exists for provider-less runs.
"""

import json
from pathlib import Path

from jinlin_agent.config import load_config
from jinlin_agent.events import read_events
from jinlin_agent.ledger import TaskLedger
from jinlin_agent.model.mock import MockProvider
from jinlin_agent.sandbox import NullSandbox
from jinlin_agent.supervisor import assess_complexity, scan_workspace
from jinlin_agent.taskmodel import Budget, TaskSpec, new_task_id
from jinlin_agent.worker_runtime import WorkerRuntime

from test_worker_runtime import make_ws, read_only_script


# --------------------------------------------------------------------------- helpers


def make_stack(tmp_path, worker_scripts, supervisor_script=None, max_replans: int | None = None):
    cfg = load_config(home=tmp_path / "home")
    if max_replans is not None:
        cfg.runtime["max_replans"] = max_replans
    ledger = TaskLedger(cfg).open()
    worker_iter = iter(worker_scripts)
    runtime = WorkerRuntime(
        cfg,
        ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda ws: NullSandbox(str(ws)),
        log=lambda msg: None,
    )
    supervisor_provider = MockProvider(supervisor_script) if supervisor_script is not None else None
    from jinlin_agent.supervisor import Supervisor

    supervisor = Supervisor(cfg, ledger, runtime, provider=supervisor_provider, log=lambda msg: None)
    return cfg, ledger, runtime, supervisor


def make_root(ledger: TaskLedger, title, description="") -> TaskSpec:
    tid = new_task_id()
    spec = TaskSpec(
        task_id=tid,
        root_task_id=tid,
        title=title,
        description=description,
        role="worker",
        capabilities=["READ"],
        budget=Budget(max_tokens=300_000, max_tool_calls=200, timeout_seconds=3600, max_attempts=1),
    )
    ledger.create_task(spec)
    return spec


def plan_step(subtasks: list[dict]) -> dict:
    return {"text": "```json\n" + json.dumps({"subtasks": subtasks}) + "\n```"}


def assess_step(strategy: str = "plan_then_execute") -> dict:
    return {"text": json.dumps({"strategy": strategy, "reasons": ["scripted assessment"]})}


def sub(title: str, depends_on: list[str] | None = None) -> dict:
    return {
        "title": title,
        "description": f"do {title}",
        "depends_on": depends_on or [],
        "role": "worker",
        "capabilities": ["READ"],
        "acceptance_criteria": [f"{title} done"],
    }


def by_title(ledger: TaskLedger, root_id: str, title: str) -> dict:
    rows = [r for r in ledger.list_tasks(root_id) if r["title"] == title]
    assert rows, f"task {title!r} not registered"
    return rows[0]


# --------------------------------------------------------------------------- scanner & assessment


def test_scan_workspace_objective_signals(tmp_path):
    ws = make_ws(tmp_path / "ws")
    (ws / "tests").mkdir()
    (ws / "tests" / "test_app.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    cfg = load_config(home=tmp_path / "home")
    scan = scan_workspace(ws, cfg)
    assert scan["file_count"] >= 3
    assert "python" in scan["languages"]
    assert scan["has_tests"] is True
    assert "git_repo" in scan


def test_deterministic_assessment_defaults_to_direct(tmp_path):
    cfg = load_config(home=tmp_path / "home")
    scan = scan_workspace(make_ws(tmp_path / "ws"), cfg)
    direct = assess_complexity("fix the typo in readme", scan, provider=None)
    assert direct.strategy == "direct"
    planned = assess_complexity("1) refactor config\n2) update tests\n3) rerun suite", scan, provider=None)
    assert planned.strategy in ("plan_then_execute", "parallel_workers", "sequential_workers")


def test_model_assessment_is_used_when_provider_present(tmp_path):
    cfg = load_config(home=tmp_path / "home")
    scan = scan_workspace(make_ws(tmp_path / "ws"), cfg)
    provider = MockProvider([{"text": json.dumps({"strategy": "human_input_required", "reasons": ["goal ambiguous"]})}])
    assessment = assess_complexity("do the thing", scan, provider=provider)
    assert assessment.strategy == "human_input_required"
    assert "goal ambiguous" in assessment.reasons


# --------------------------------------------------------------------------- direct & planned execution


def test_direct_strategy_never_plans_or_creates_subtasks(tmp_path):
    cfg, ledger, runtime, supervisor = make_stack(tmp_path, [read_only_script()])
    root = make_root(ledger, "explain the entry point")
    try:
        outcome = supervisor.execute_root(root, make_ws(tmp_path / "ws"))
        assert outcome["status"] == "completed"
        assert ledger.list_tasks(root.task_id) and len(ledger.list_tasks(root.task_id)) == 1  # no subtasks
        events = [e["type"] for e in read_events(cfg.tasks_dir / root.task_id)]
        assert "task.strategy_selected" in events
    finally:
        runtime.shutdown()
        ledger.close()


def test_planned_dag_respects_dependency_order(tmp_path):
    plan = plan_step(
        [
            sub("investigate modules"),
            sub("write part a", ["investigate modules"]),
            sub("write part b", ["investigate modules"]),
            sub("integrate results", ["write part a", "write part b"]),
        ]
    )
    cfg, ledger, runtime, supervisor = make_stack(
        tmp_path,
        [read_only_script()] * 4,
        supervisor_script=[assess_step(), plan],
    )
    root = make_root(ledger, "1) investigate\n2) write parts\n3) integrate")
    try:
        outcome = supervisor.execute_root(root, make_ws(tmp_path / "ws"))
        assert outcome["status"] == "completed", outcome

        # dependency order: A after investigate, B after investigate, integrate last
        order = {}
        for title in ("investigate modules", "write part a", "write part b", "integrate results"):
            row = by_title(ledger, root.task_id, title)
            assert row["status"] == "completed"
            order[title] = row["updated_at"]
        assert order["write part a"] >= order["investigate modules"]
        assert order["write part b"] >= order["investigate modules"]
        assert order["integrate results"] >= order["write part a"]
        assert order["integrate results"] >= order["write part b"]
        assert ledger.get_task(root.task_id)["status"] == "completed"
    finally:
        runtime.shutdown()
        ledger.close()


def test_cycle_in_planned_dag_fails_root_honestly(tmp_path):
    bad = plan_step(
        [
            sub("a", ["b"]),
            sub("b", ["a"]),
        ]
    )
    cfg, ledger, runtime, supervisor = make_stack(tmp_path, [], supervisor_script=[assess_step(), bad])
    root = make_root(ledger, "1) a\n2) b")
    try:
        outcome = supervisor.execute_root(root, make_ws(tmp_path / "ws"))
        assert outcome["status"] == "failed"
        assert "cycle" in json.dumps(outcome).lower()
        assert ledger.get_task(root.task_id)["status"] == "failed"
    finally:
        runtime.shutdown()
        ledger.close()


# --------------------------------------------------------------------------- replanning


def test_failure_triggers_bounded_local_replan(tmp_path):
    plan = plan_step([sub("flaky step"), sub("downstream step", ["flaky step"])])
    replan = plan_step([sub("flaky step (retry)"), sub("downstream step (follow-up)", ["flaky step (retry)"])])
    cfg, ledger, runtime, supervisor = make_stack(
        tmp_path,
        [[{"raise": {"code": "MODEL_CONNECTION", "message": "flaky", "retriable": True}}], read_only_script(), read_only_script()],
        supervisor_script=[assess_step(), plan, replan],
    )
    root = make_root(ledger, "1) flaky step\n2) downstream step")
    try:
        outcome = supervisor.execute_root(root, make_ws(tmp_path / "ws"))
        assert outcome["status"] == "completed", outcome

        old = by_title(ledger, root.task_id, "flaky step")
        assert old["status"] == "failed"
        replacement = by_title(ledger, root.task_id, "flaky step (retry)")
        assert replacement["status"] == "completed"
        # the stale non-terminal downstream of the failed node was cancelled
        down_old = by_title(ledger, root.task_id, "downstream step")
        assert down_old["status"] == "cancelled"
        # the replacement carries the downstream goal forward
        down_new = by_title(ledger, root.task_id, "downstream step (follow-up)")
        assert down_new["status"] == "completed"
        assert ledger.get_task(root.task_id)["status"] == "completed"

        events = [e["type"] for e in read_events(cfg.tasks_dir / root.task_id)]
        assert events.count("task.replanned") == 1
    finally:
        runtime.shutdown()
        ledger.close()


def test_max_replans_zero_stops_without_replanning(tmp_path):
    plan = plan_step([sub("always fails")])
    cfg, ledger, runtime, supervisor = make_stack(
        tmp_path,
        [[{"raise": {"code": "MODEL_CONNECTION", "message": "boom", "retriable": True}}]],
        supervisor_script=[assess_step(), plan],
        max_replans=0,
    )
    root = make_root(ledger, "1) always fails")
    try:
        outcome = supervisor.execute_root(root, make_ws(tmp_path / "ws"))
        assert outcome["status"] == "failed"
        assert ledger.get_task(root.task_id)["status"] == "failed"
        events = [e["type"] for e in read_events(cfg.tasks_dir / root.task_id)]
        assert "task.replanned" not in events
    finally:
        runtime.shutdown()
        ledger.close()


def test_replans_beyond_budget_stop_the_root(tmp_path):
    plan = plan_step([sub("always fails")])
    replan = plan_step([sub("also fails")])
    cfg, ledger, runtime, supervisor = make_stack(
        tmp_path,
        [[{"raise": {"code": "MODEL_CONNECTION", "message": "boom", "retriable": True}}]] * 4,
        supervisor_script=[assess_step(), plan, replan, replan],
        max_replans=2,
    )
    root = make_root(ledger, "1) always fails")
    try:
        outcome = supervisor.execute_root(root, make_ws(tmp_path / "ws"))
        assert outcome["status"] == "failed"
        events = [e["type"] for e in read_events(cfg.tasks_dir / root.task_id)]
        assert events.count("task.replanned") == 2  # exactly max_replans, then stop
        assert ledger.get_task(root.task_id)["status"] == "failed"
    finally:
        runtime.shutdown()
        ledger.close()


# --------------------------------------------------------------------------- human input


def test_waiting_subtask_surfaces_and_resumes(tmp_path):
    ask_script = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "request_user_input", "arguments": {"question": "which format?"}}]},
    ]
    plan = plan_step([sub("need user decision")])
    cfg, ledger, runtime, supervisor = make_stack(
        tmp_path,
        [ask_script, read_only_script()],
        supervisor_script=[assess_step(), plan],
    )
    root = make_root(ledger, "1) need user decision")
    try:
        outcome = supervisor.execute_root(root, make_ws(tmp_path / "ws"))
        assert outcome["status"] == "waiting_user"
        assert outcome.get("worker_id")

        runtime.resume_worker(outcome["worker_id"], answer="use markdown")
        second = supervisor.execute_root(root, make_ws(tmp_path / "ws"))
        assert second["status"] == "completed"
        assert ledger.get_task(root.task_id)["status"] == "completed"
    finally:
        runtime.shutdown()
        ledger.close()
