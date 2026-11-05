"""WP-F1/F2/F3 regression: three genuinely different benchmark strategies and
honest dry-run execution.

* single  = one Harness session (v1 path): NO ledger task, NO planner.
* planner = Planner decomposes, execution_mode="sequential" chains the
  subtasks so exactly one worker runs them in order (deps are rewritten to a
  linear chain — asserted from the ledger).
* multi   = Supervisor schedules the planned DAG; parallelism allowed.

All strategies run the same workspace state; dry-run drives the REAL
pipeline (real TaskSpec/ledger/workers/gates) with a scripted provider.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from benchmark_matrix import aggregate, run_one  # noqa: E402


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text("# demo\n\nEntry point: app.main()\n", encoding="utf-8")
    (ws / "app.py").write_text("def main():\n    return 'served'\n", encoding="utf-8")
    return ws


def make_git_workspace(tmp_path: Path) -> Path:
    import subprocess

    ws = make_workspace(tmp_path)
    for argv in (
        ["init", "-q"],
        ["-c", "user.email=t@e.com", "-c", "user.name=t", "add", "-A"],
        ["-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-qm", "i"],
    ):
        subprocess.run(["git", *argv], cwd=str(ws), capture_output=True)
    return ws


TASK = {"id": "analyze-demo", "prompt": "analyze the entry point"}


def test_single_strategy_has_no_ledger_task(tmp_path):
    from jinlin_agent.config import load_config

    record = run_one(TASK, "single", dry_run=True, out_dir=tmp_path)
    assert record["status"] == "completed"
    assert record["ledger_task"] is False
    # WP-C1: the read-only dry-run session never runs the verification
    # ladder (nothing changed), so the honest verdict is not_run / False —
    # the pre-C1 code reported True here via any() over an empty list
    assert record["verification_status"] == "not_run"
    assert record["verification_passed"] is False
    # no ledger task was created anywhere
    tasks_dir = tmp_path / "home" / "tasks"
    assert not tasks_dir.exists() or not list(tasks_dir.iterdir())


def test_planner_strategy_chains_subtasks_for_one_worker(tmp_path):
    from jinlin_agent.config import load_config
    from jinlin_agent.ledger import TaskLedger

    record = run_one(TASK, "planner", dry_run=True, out_dir=tmp_path)
    assert record["status"] == "completed", record
    assert record["ledger_task"] is True
    assert record["subtask_integration"] == ["not_needed", "not_needed"]  # analysis tasks

    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    try:
        rows = [r for r in ledger.list_tasks() if r["parent_task_id"]]  # children only
        subs = sorted(rows, key=lambda r: r["title"])
        assert len(subs) == 2
        # the sequential chain: each subtask depends on its predecessor
        by_title = {r["title"]: r for r in subs}
        second = by_title["analyze part B"]
        assert by_title["analyze part A"]["task_id"] in second["dependencies"], (
            "sequential mode must chain the planned subtasks"
        )
    finally:
        ledger.close()


def test_multi_strategy_schedules_the_planned_dag(tmp_path):
    from jinlin_agent.config import load_config
    from jinlin_agent.ledger import TaskLedger

    record = run_one(TASK, "multi", dry_run=True, out_dir=tmp_path)
    assert record["status"] == "completed", record
    assert record["ledger_task"] is True
    # the planned DAG keeps its planned (empty) dependencies — parallel-ready
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    try:
        subs = [r for r in ledger.list_tasks() if r["title"].startswith("analyze part")]
        assert len(subs) == 2
        assert all(r["dependencies"] == [] for r in subs), "multi mode must keep the planned DAG"
    finally:
        ledger.close()


def test_multi_strategy_integrates_via_worktree_on_a_git_repo(tmp_path):
    """multi on a git repo attaches a WorkspaceManager: every worker gets its
    own worktree, and a read-only deliverable must be reported not_needed —
    never merged as an empty commit nor re-verified with the project suite
    (the regression that failed the first dry-run against a real checkout)."""
    ws = make_git_workspace(tmp_path)
    task = {**TASK, "workspace": str(ws)}
    record = run_one(task, "multi", dry_run=True, out_dir=tmp_path)
    assert record["status"] == "completed", record
    # integration state: analysis tasks have no changes -> not_needed
    assert record["subtask_integration"] == ["not_needed", "not_needed"]
    # WP-C1: the subtasks declare the 'analysis' profile (no required
    # levels) and the run made no changes — the honest verdict is
    # not_applicable, which is explicitly NOT a pass
    assert record["verification_status"] == "not_applicable"
    assert record["verification_passed"] is False


def test_strategies_produce_distinct_ledger_footprints(tmp_path):
    """WP-F1 evidence: the three paths are really different code paths."""
    from jinlin_agent.config import load_config
    from jinlin_agent.ledger import TaskLedger

    cfg = load_config(home=tmp_path / "home")
    single = run_one(TASK, "single", dry_run=True, out_dir=tmp_path)
    assert single["ledger_task"] is False
    planner = run_one(TASK, "planner", dry_run=True, out_dir=tmp_path)
    multi = run_one(TASK, "multi", dry_run=True, out_dir=tmp_path)
    ledger = TaskLedger(cfg).open()
    try:
        # planner + multi each created a root task (plus its 2 subtasks);
        # single created no ledger rows at all
        roots = [r for r in ledger.list_tasks() if not r["parent_task_id"]]
        assert len(roots) == 2
        assert ledger.summary()["total"] == 6
        assert planner["ledger_task"] is True and multi["ledger_task"] is True
    finally:
        ledger.close()


def test_metrics_are_recorded_per_run(tmp_path):
    record = run_one(TASK, "single", dry_run=True, out_dir=tmp_path)
    for key in ("task_id", "strategy", "status",
                "verification_status", "verification_passed", "verification_evidence",
                "changed_files", "wall_seconds",
                "tokens_in_actual", "tokens_out_actual", "tokens_total_actual",
                "tokens_estimated_context", "tokens_usage_missing_calls", "usage_complete",
                "model_calls", "model_attempts", "tool_calls",
                "replans", "conflicts", "human_interventions", "error_summary"):
        assert key in record, f"missing metric {key}"
    summary = aggregate([record])
    # WP-C2: dry-run has no provider usage — the aggregate reports that
    # honestly instead of a token number
    assert summary["single"]["runs_with_usage"] == 0


# --------------------------------------------------------------------------- WP-C1: benchmark-level verification semantics


def test_completed_status_does_not_imply_verification_passed(tmp_path):
    """The third-round defect, at benchmark level: a session that COMPLETED
    but holds no verification records must report not_run / False."""
    from types import SimpleNamespace

    from benchmark_matrix import _session_metrics

    state = SimpleNamespace(
        verification_runs=[],
        diff_paths=lambda: [],
        model_attempts=2,
        tokens_usage_missing_calls=2,
    )
    metrics = _session_metrics(state)  # no status input exists at all
    assert metrics["verification_status"] == "not_run"
    assert metrics["verification_passed"] is False


def test_single_strategy_aggregates_session_verification_events(tmp_path, monkeypatch):
    """WP-C1 rule 8 with actual records: single has no ledger task, so its
    verdict is rebuilt from the SESSION event log's verification records.
    A session that CHANGES a file runs the ladder; a read-only session with
    no records must stay not_run (see the no-ledger-task test)."""
    from jinlin_agent.model import mock as mock_module
    from jinlin_agent.model.mock import MockProvider as RealMockProvider

    writing_script = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {"tool_calls": [{"name": "write_file", "arguments": {"path": "notes.md", "content": "# analysis notes\n"}}]},
        {"tool_calls": [{"name": "propose_completion", "arguments": {"summary": "notes written", "claims": [{"statement": "notes.md written", "evidence_refs": ["notes.md"]}]}}]},
    ]
    monkeypatch.setattr(mock_module, "MockProvider", lambda script=None: RealMockProvider(writing_script))
    record = run_one(TASK, "single", dry_run=True, out_dir=tmp_path)
    evidence = record["verification_evidence"]
    assert evidence["executed_levels"], "the ladder must have produced verification records"
    assert record["verification_passed"] == (record["verification_status"] == "passed")


def test_supervised_strategy_does_not_infer_verification_from_outcome_status():
    """A completed outcome plus missing integration evidence aggregates to
    not_run — the outcome never initializes the root verdict."""
    from benchmark_matrix import _aggregate_verification, _integration_evidence

    passed_worker = {
        "verification_status": "passed",
        "verification_passed": True,
        "verification_evidence": {
            "status": "passed", "required_levels": ["L0"], "executed_levels": ["L0"],
            "failed_levels": [], "skipped_levels": [], "notes": [],
        },
    }
    root = _aggregate_verification([passed_worker, _integration_evidence([], required=True)])
    assert root["verification_status"] == "not_run"
    assert root["verification_passed"] is False


def test_root_verification_requires_all_required_worker_and_integration_evidence():
    """WP-C1 rule 6: one worker's pass never upgrades a failed/missing
    sibling or a failed integration; all-pass upgrades the root."""
    from benchmark_matrix import _aggregate_verification

    def comp(status):
        return {
            "verification_status": status,
            "verification_passed": status == "passed",
            "verification_evidence": {
                "status": status, "required_levels": ["L0"], "executed_levels": ["L0"] if status == "passed" else [],
                "failed_levels": ["L0"] if status == "failed" else [], "skipped_levels": [], "notes": [],
            },
        }

    assert _aggregate_verification([comp("passed"), comp("failed")])["verification_status"] == "failed"
    assert _aggregate_verification([comp("passed"), comp("not_run")])["verification_status"] == "not_run"
    assert _aggregate_verification([comp("passed"), comp("passed")])["verification_passed"] is True
    assert _aggregate_verification([comp("not_applicable"), comp("not_applicable")])["verification_status"] == "not_applicable"
