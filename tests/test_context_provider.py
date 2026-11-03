"""WP-V2.0-7 regression: context provider interface + dependency deliverable
injection (v2 §8.2/§8.3).

The worker's context is assembled through ContextProvider hooks: dependency
tasks' recorded deliverables are scored into the window like any other block
(budget pressure drops them WITH a recorded reason, never silently), and the
worker must be able to see what upstream produced without re-deriving it.
"""

import json
from pathlib import Path

from jinlin_agent.config import load_config
from jinlin_agent.context import ContextBuilder
from jinlin_agent.context_provider import DependencyContextProvider
from jinlin_agent.ledger import TaskLedger
from jinlin_agent.taskmodel import Budget, TaskSpec, new_task_id


class _Caps:
    context_window = 5000
    max_output_tokens = 512


class _State:
    session_id = "sess_x"
    step_index = 0
    phase = "IMPLEMENTING"
    model_calls = 0
    tool_calls_total = 0
    compactions = 0
    request = "use upstream results"
    user_messages: list = []
    plan = None
    facts: list = []
    decisions: list = []
    verification_runs: list = []
    recent_observations: list = []
    read_hashes: dict = {}
    written_files: dict = {}
    deleted_files: list = []
    workspace = "."


class _Artifacts:
    def write_json(self, kind, data, meta=None):
        class _A:
            artifact_id = "artifact_x"

        return _A()


class _Sandbox:
    level = "L0"
    engine = "none"

    def run(self, *a, **k):
        class _R:
            exit_code = 1
            stdout = ""

        return _R()


def make_ledger(tmp_path):
    cfg = load_config(home=tmp_path / "home")
    return TaskLedger(cfg).open(), cfg


def test_provider_returns_blocks_for_completed_dependencies(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    dep = TaskSpec(task_id=new_task_id(), root_task_id="root_x", title="upstream analysis")
    ledger.create_task(dep)
    ledger.set_task_status(dep.task_id, "running")
    ledger.record_result(dep.task_id, {"status": "completed", "summary": "found the bug in loader.py", "changed_files": ["loader.py"]})
    ledger.set_task_status(dep.task_id, "completed")

    provider = DependencyContextProvider(ledger, dependencies=[dep.task_id])
    blocks = provider.blocks(_State(), budget=4000)
    assert blocks, "completed dependency deliverable must produce a context block"
    body = blocks[0].content
    assert "found the bug in loader.py" in body
    assert blocks[0].block_id.startswith("dependency:")
    ledger.close()


def test_provider_skips_missing_or_unfinished_dependencies(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    unfinished = TaskSpec(task_id=new_task_id(), root_task_id="root_x", title="still running")
    ledger.create_task(unfinished)
    ledger.set_task_status(unfinished.task_id, "running")

    provider = DependencyContextProvider(ledger, dependencies=["task_missing", unfinished.task_id])
    assert provider.blocks(_State(), budget=4000) == []
    ledger.close()


def test_dependency_blocks_survive_normal_budget_and_drop_under_pressure(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    dep = TaskSpec(task_id=new_task_id(), root_task_id="root_x", title="upstream")
    ledger.create_task(dep)
    ledger.set_task_status(dep.task_id, "running")
    ledger.record_result(dep.task_id, {"status": "completed", "summary": "R" * 6000, "changed_files": []})
    ledger.set_task_status(dep.task_id, "completed")
    provider = DependencyContextProvider(ledger, dependencies=[dep.task_id])

    builder = ContextBuilder(
        config=load_config(home=tmp_path / "home"),
        capabilities=_Caps(),  # tiny window -> pressure
        artifacts=_Artifacts(),
        sandbox=_Sandbox(),
        providers=[provider],
    )
    bundle = builder.build(_State(), history=[], user_request="use upstream")
    ids = [b.block_id for b in bundle.blocks if b.block_id.startswith("dependency:")]
    dropped_ids = [d["id"] for d in bundle.dropped if str(d["id"]).startswith("dependency:")]
    assert ids or dropped_ids, "dependency block must be either kept with budget or dropped with a reason"
    if dropped_ids:
        assert all(d.get("reason") for d in bundle.dropped if str(d["id"]).startswith("dependency:"))
    ledger.close()


def test_worker_sees_upstream_deliverable_in_its_context(tmp_path):
    from jinlin_agent.model.mock import MockProvider
    from jinlin_agent.sandbox import NullSandbox
    from jinlin_agent.worker_runtime import WorkerRuntime

    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text("# demo\n", encoding="utf-8")

    dep = TaskSpec(task_id=new_task_id(), root_task_id="root_x", title="upstream analysis")
    ledger.create_task(dep)
    ledger.set_task_status(dep.task_id, "running")
    ledger.record_result(dep.task_id, {"status": "completed", "summary": "the bug lives in calc.add", "changed_files": []})
    ledger.set_task_status(dep.task_id, "completed")

    captured: dict = {}

    def capturing_step(messages):
        # MockProvider hands callables the messages as dicts
        captured["first"] = [m.get("content") for m in messages]
        return {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]}

    finish = [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {"plan": {"objective": "use upstream", "steps": [{"description": "read", "status": "completed", "evidence": ["README.md"]}]}},
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
    script = [capturing_step] + finish
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
        title="downstream work",
        capabilities=["READ"],
        dependencies=[dep.task_id],
        budget=Budget(max_tokens=300_000, max_tool_calls=200, timeout_seconds=3600, max_attempts=1),
    )
    ledger.create_task(spec)
    try:
        worker_id = runtime.submit_task(tid, ws)
        outcome = runtime.wait_worker(worker_id)
        assert outcome["status"] == "completed", outcome
        joined = "\n".join(captured["first"])
        assert "the bug lives in calc.add" in joined, "worker must see the upstream deliverable"
    finally:
        runtime.shutdown()
        ledger.close()
