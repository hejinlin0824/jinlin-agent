"""WP-V2.0-8 regression: task-level verification planning and gate (v2 §10).

The verification profile declared in the TaskSpec turns into REQUIRED
verification levels; a completed worker deliverable that lacks a required
passing level is rejected by the supervisor (bounded replan can replace it).
Budget exhaustion already fails the task honestly (never 'unverified
success'). VerificationRun gains sandbox_level evidence.
"""

import pytest

from jinlin_agent.config import load_config
from jinlin_agent.sandbox import NullSandbox
from jinlin_agent.taskmodel import Budget, TaskSpec, new_task_id
from jinlin_agent.verify import VerificationRun, check_task_verification, plan_task_verification


def spec_with(verification: str) -> TaskSpec:
    tid = new_task_id()
    return TaskSpec(
        task_id=tid,
        root_task_id=tid,
        title="t",
        verification=verification,
        budget=Budget(max_tokens=300_000, max_tool_calls=200, timeout_seconds=3600, max_attempts=1),
    )


def result_with(*levels: str) -> dict:
    return {
        "status": "completed",
        "summary": "did the work",
        "changed_files": ["a.py"],
        "tests": [{"command": f"cmd for {lvl}", "level": lvl, "status": status} for lvl, status in levels],
    }


def test_plan_task_verification_profiles():
    assert plan_task_verification(spec_with("code")) == ["L0", "L1", "L3"]
    assert plan_task_verification(spec_with("config")) == ["L0", "L1"]
    assert plan_task_verification(spec_with("docs")) == ["L0"]
    assert plan_task_verification(spec_with("tests")) == ["L3"]
    assert plan_task_verification(spec_with("analysis")) == []


def test_unknown_profile_falls_back_to_code_with_reason():
    reasons = check_task_verification(spec_with("mystery"), result_with(("L3", "passed")))
    assert any("unknown" in r.lower() for r in reasons)


def test_gate_passes_when_required_levels_passed():
    assert check_task_verification(spec_with("code"), result_with(("L0", "passed"), ("L1", "passed"), ("L3", "passed"))) == []


def test_gate_rejects_missing_or_failed_required_levels():
    reasons = check_task_verification(spec_with("code"), result_with(("L0", "passed"), ("L1", "passed")))
    assert any("L3" in r for r in reasons)
    reasons = check_task_verification(spec_with("code"), result_with(("L0", "passed"), ("L1", "passed"), ("L3", "failed")))
    assert any("L3" in r for r in reasons)
    reasons = check_task_verification(
        spec_with("code"), {"status": "completed", "summary": "x", "changed_files": ["a.py"], "tests": []}
    )
    assert len(reasons) == 3  # changes present: L0, L1, L3 all required and missing


def test_l3_applicability_depends_on_the_project():
    # no test command in the project: 'code' records L3 as not applicable...
    reasons = check_task_verification(spec_with("code"), result_with(("L0", "passed"), ("L1", "passed")), has_test_command=False)
    assert reasons == []
    # ...but the 'tests' profile DEMANDS L3: un-verifiable is an explicit failure (v2 §10.4)
    reasons = check_task_verification(spec_with("tests"), {"status": "completed", "summary": "x", "tests": []}, has_test_command=False)
    assert any("L3" in r and "cannot be verified" in r for r in reasons)


def test_analysis_profile_needs_no_code_levels():
    assert check_task_verification(spec_with("analysis"), {"status": "completed", "summary": "x", "tests": []}) == []


# --------------------------------------------------------------------------- evidence fields


def test_verification_run_records_sandbox_level(tmp_path):
    cfg = load_config(home=tmp_path / "home")
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    sandbox = NullSandbox(str(ws))
    events: list[tuple[str, dict]] = []

    from jinlin_agent.verify import Verifier

    verifier = Verifier(ws, cfg, sandbox, artifacts=None, emit=lambda t, p: events.append((t, p)), state=type("S", (), {"written_files": {}, "deleted_files": []})())
    run = verifier.run_level("L0")
    assert run.sandbox_level == "L0"


# --------------------------------------------------------------------------- supervisor gate


def _read_only_script():
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


def _breaking_script():
    return [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
        {"tool_calls": [{"name": "update_plan", "arguments": {"plan": {"objective": "fix", "steps": [{"description": "edit", "status": "completed", "evidence": ["calc.py"]}]}}}]},
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old_string": "return a + b", "new_string": "return a + b + 1"}}]},
        {"tool_calls": [{"name": "propose_completion", "arguments": {"summary": "fixed the adder", "claims": [{"statement": "tests pass now", "evidence_refs": ["calc.py"]}]}}]},
    ]


def _plan_json(subtasks):
    import json as _json

    return {"text": "```json\n" + _json.dumps({"subtasks": subtasks}) + "\n```"}


def _assess():
    import json as _json

    return {"text": _json.dumps({"strategy": "plan_then_execute", "reasons": ["s"]})}


def test_supervisor_rejects_deliverable_missing_required_verification(tmp_path):
    """A 'code' task whose tests fail is not completed: the task-level gate
    fails it, and the bounded replan replaces it with an analysis task."""
    import json as _json

    from pathlib import Path as _P

    from jinlin_agent.ledger import TaskLedger
    from jinlin_agent.model.mock import MockProvider
    from jinlin_agent.sandbox import NullSandbox
    from jinlin_agent.supervisor import Supervisor
    from jinlin_agent.taskmodel import new_task_id
    from jinlin_agent.worker_runtime import WorkerRuntime
    from test_worktree import make_git_repo

    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (repo / "README.md").write_text("# calc\n", encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    import subprocess as _sp

    for args in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "init"]):
        _sp.run(["git", "-c", "user.email=t@e.com", "-c", "user.name=t", *args], cwd=str(repo), capture_output=True)
    # no test command in this project: the session gate cannot require L3 —
    # the task-level gate is the one that catches the missing required level
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    worker_iter = iter([_breaking_script(), _read_only_script()])
    runtime = WorkerRuntime(
        cfg,
        ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda w: NullSandbox(str(w)),
        log=lambda msg: None,
    )
    plan = _plan_json(
        [
            # the project has NO test command, yet this task demands L3:
            # the acceptance gate fails it with the explicit reason
            {"title": "fix adder", "description": "fix", "depends_on": [], "role": "worker", "capabilities": ["READ", "WRITE"], "acceptance_criteria": ["adder correct"], "verification": "tests"},
            {"title": "summarize", "description": "analyze", "depends_on": ["fix adder"], "role": "worker", "capabilities": ["READ"], "acceptance_criteria": ["summary"], "verification": "analysis"},
        ]
    )
    replan = _plan_json(
        [
            {"title": "fix adder (analysis fallback)", "description": "analyze instead", "depends_on": [], "role": "worker", "capabilities": ["READ"], "acceptance_criteria": ["analysis"], "verification": "analysis"},
        ]
    )
    supervisor = Supervisor(cfg, ledger, runtime, provider=MockProvider([_assess(), plan, replan]), log=lambda msg: None)
    tid = new_task_id()
    root = TaskSpec(task_id=tid, root_task_id=tid, title="1) fix adder\n2) summarize", capabilities=["READ", "WRITE"])
    ledger.create_task(root)
    try:
        outcome = supervisor.execute_root(root, repo)
        assert outcome["status"] == "completed", outcome
        failed_rows = [r for r in ledger.list_tasks(tid) if r["status"] == "failed"]
        assert failed_rows and "verification" in " ".join(r.get("error", "") for r in failed_rows)
        completed = [r for r in ledger.list_tasks(tid) if r["status"] == "completed" and r["task_id"] != tid]
        assert completed
    finally:
        runtime.shutdown()
        ledger.close()
