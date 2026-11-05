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
    assert record["verification_passed"] is True
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
    assert record["verification_passed"] is True


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
    for key in ("task_id", "strategy", "status", "verification_passed", "changed_files",
                "wall_seconds", "tokens_in", "tokens_out", "model_calls", "tool_calls",
                "replans", "conflicts", "human_interventions", "error_summary"):
        assert key in record, f"missing metric {key}"
    summary = aggregate([record])
    assert summary["single"]["verification_pass_rate"] == 1.0
