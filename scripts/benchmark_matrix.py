"""WP-R10/F1-F3: real-task benchmark matrix runner.

Three strategies with GENUINELY DIFFERENT execution paths (WP-F1):

* ``single``  — the v1 path: one Harness session drives the whole task
  directly (cli `run` semantics). No planner, no ledger task, no workers.
* ``planner`` — the Planner decomposes the task, but execution is strictly
  ONE worker in plan order (Supervisor execution_mode="sequential": the
  planned DAG is chained linearly).
* ``multi``   — the Supervisor schedules the planned DAG, running every
  dependency-satisfied subtask in parallel on its own worktree.

All strategies run the same task, the same initial workspace state and the
same acceptance criteria (the session verification ladder + completion gate
is identical because every worker is the same Harness kernel).

WP-F2: ``--dry-run`` substitutes the model with a deterministic MockProvider
but otherwise runs the REAL software pipeline — real TaskSpec/TaskRecord in
the ledger, real workers/sessions, real verification ladder and completion
gate, real integration. Only the model responses are scripted, so no tokens
are consumed.

WP-F3: every run records task_id, strategy, status, verification_passed,
changed_files, wall_seconds, tokens_in/out, model_calls, tool_calls, replans,
conflicts, human_interventions and error_summary. `status` is the EXECUTION
outcome; `verification_passed` is computed independently from the recorded
verification runs (never inferred from status alone).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

STRATEGIES = ("single", "planner", "multi")

# Deterministic script used by --dry-run: the same flow the offline E1 probe
# exercises, so the dry run passes the real completion gate without tokens.
_DRY_RUN_SCRIPT = [
    {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
    {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
    {
        "tool_calls": [
            {
                "name": "update_plan",
                "arguments": {"plan": {"objective": "analyze", "steps": [{"description": "read", "status": "completed", "evidence": ["README.md"]}]}},
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

_ASSESS_DIRECT = {"text": json.dumps({"strategy": "direct", "reasons": ["dry-run single"]})}
_ASSESS_PLAN = {"text": json.dumps({"strategy": "plan_then_execute", "reasons": ["dry-run plan"]})}
_DRY_PLAN = {
    "text": "```json\n"
    + json.dumps(
        {
            "subtasks": [
                {"title": "analyze part A", "description": "read and analyze A", "depends_on": [], "role": "worker", "capabilities": ["READ"], "acceptance_criteria": ["A analyzed"], "verification": "analysis"},
                {"title": "analyze part B", "description": "read and analyze B", "depends_on": [], "role": "worker", "capabilities": ["READ"], "acceptance_criteria": ["B analyzed"], "verification": "analysis"},
            ]
        }
    )
    + "\n```"
}


def load_tasks(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("tasks file must be a JSON list")
    for item in data:
        for key in ("id", "prompt"):
            if key not in item:
                raise SystemExit(f"task entry missing {key!r}: {item}")
    return data


def _ensure_workspace(task: dict[str, Any], dry_run: bool, out_dir: Path) -> Path:
    """WP-F1: all strategies run against the SAME workspace state."""
    raw = str(task.get("workspace") or "").strip()
    if raw:
        workspace = Path(raw).expanduser()
    else:
        # no explicit workspace: never fall back to the CWD — the run owns a
        # scratch workspace under the output directory (dry-run materializes
        # the standard demo state so every strategy sees the identical initial
        # state; a real run must point at an existing checkout)
        workspace = Path(out_dir) / "workspaces" / task["id"]
    if not workspace.exists():
        if not dry_run:
            raise SystemExit(f"workspace does not exist: {workspace} (task {task['id']})")
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "README.md").write_text("# demo\n\nEntry point: app.main()\n", encoding="utf-8")
        (workspace / "app.py").write_text("def main():\n    return 'served'\n", encoding="utf-8")
        # the scratch workspace is its OWN tiny repo (never a subdirectory of
        # another repo — the workspace manager only attaches to repo ROOTS):
        # multi then exercises the real worktree/integration path in dry-run
        import subprocess as _sp

        for argv in (
            ["init", "-q"],
            ["-c", "user.email=benchmark@local", "-c", "user.name=benchmark", "add", "-A"],
            ["-c", "user.email=benchmark@local", "-c", "user.name=benchmark", "commit", "-qm", "demo state"],
        ):
            _sp.run(["git", *argv], cwd=str(workspace), capture_output=True)
    return workspace


def _session_metrics(state: Any) -> dict[str, Any]:
    runs = list(getattr(state, "verification_runs", []) or [])
    verification_failed = any(r.status == "failed" for r in runs)
    required_failed = any(
        r.status in ("failed",) for r in runs if r.level in ("L0", "L1", "L3")
    )
    return {
        "tokens_in": int(getattr(state, "tokens_in", 0) or 0),
        "tokens_out": int(getattr(state, "tokens_out", 0) or 0),
        "model_calls": int(getattr(state, "model_calls", 0) or 0),
        "tool_calls": int(getattr(state, "tool_calls_total", 0) or 0),
        "changed_files": state.diff_paths(),
        # WP-F3: verification_passed is computed from verification RUNS, never
        # from the completion status alone
        "verification_passed": (not verification_failed) and (not required_failed),
    }


def _human_interventions(events: list[dict[str, Any]]) -> int:
    return sum(
        1 for e in events if e.get("type") in ("session.waiting_user", "approval.requested")
    )


# --------------------------------------------------------------------------- strategies


def run_single(task: dict[str, Any], workspace: Path, dry_run: bool, out_dir: Path) -> dict[str, Any]:
    """v1 path: one Harness session, no ledger task, no planner, no workers."""
    from jinlin_agent.config import load_config
    from jinlin_agent.loop import Harness
    from jinlin_agent.model import create_provider
    from jinlin_agent.model.mock import MockProvider
    from jinlin_agent.sandbox import create_sandbox
    from jinlin_agent.session import SessionManager

    cfg = load_config(workspace, home=Path(out_dir) / "home")
    provider = MockProvider(_DRY_RUN_SCRIPT) if dry_run else create_provider(cfg)
    sandbox = create_sandbox(str(workspace), cfg) if not dry_run else NullSandboxFor(str(workspace))
    session = SessionManager.create(cfg, workspace, task["prompt"], sandbox)
    harness = Harness(
        session=session, config=cfg, provider=provider, log=lambda m: None,
    )
    status = harness.run()
    state = session.state
    metrics = _session_metrics(state)
    events = list(session.log.iter_events())
    session.close()
    return {
        "status": status,
        **metrics,
        "human_interventions": _human_interventions(events),
        "error_summary": json.dumps(state.last_error, ensure_ascii=False)[:300] if state.last_error else "",
        "ledger_task": False,  # single strategy does not create a ledger task
    }


def NullSandboxFor(workspace: str):
    from jinlin_agent.sandbox import NullSandbox

    return NullSandbox(workspace)


def run_supervised(task: dict[str, Any], workspace: Path, dry_run: bool, mode: str, out_dir: Path) -> dict[str, Any]:
    """planner (sequential) / multi (parallel): ledger task + planner +
    workers + integration, via the Supervisor."""
    from jinlin_agent.config import load_config
    from jinlin_agent.ledger import TaskLedger
    from jinlin_agent.model import create_provider
    from jinlin_agent.model.mock import MockProvider
    from jinlin_agent.supervisor import Supervisor
    from jinlin_agent.taskmodel import Budget, TaskSpec, new_task_id
    from jinlin_agent.worker_runtime import WorkerRuntime
    from jinlin_agent.workspace_manager import WorkspaceManager

    cfg = load_config(workspace, home=Path(out_dir) / "home")
    ledger = TaskLedger(cfg).open()
    if dry_run:
        worker_factory = lambda: MockProvider(_DRY_RUN_SCRIPT)
        supervisor_provider = MockProvider([_ASSESS_PLAN, _DRY_PLAN])
    else:
        worker_factory = lambda: create_provider(cfg)
        supervisor_provider = create_provider(cfg)
    if mode == "sequential":
        cfg.runtime["max_concurrent_workers"] = 1
    runtime = WorkerRuntime(cfg, ledger, provider_factory=worker_factory, log=lambda m: None)
    supervisor = Supervisor(cfg, ledger, runtime, provider=supervisor_provider, log=lambda m: None, execution_mode=mode)
    manager = None
    if mode == "multi" and WorkspaceManager.is_git_repo(workspace):
        manager = WorkspaceManager(workspace, cfg, ledger)
        supervisor.attach_workspace_manager(manager)
        runtime.sandbox_factory = manager.sandbox_factory()

    tid = new_task_id()
    spec = TaskSpec(
        task_id=tid, root_task_id=tid,
        title=task["prompt"].splitlines()[0][:120],
        description=task["prompt"],
        capabilities=["READ", "WRITE", "EXECUTE"],
        budget=Budget(max_attempts=2),
    )
    ledger.create_task(spec)  # WP-F2: a real TaskRecord, even in dry-run
    try:
        outcome = supervisor.execute_root(spec, workspace)
        events: list[dict[str, Any]] = []
        log_dir = Path(cfg.tasks_dir) / tid
        if (log_dir / "events.jsonl").exists():
            from jinlin_agent.events import read_events

            events = list(read_events(log_dir))
        # aggregate session metrics across every worker attempt under this
        # root — including the root itself, which carries the worker in
        # direct mode (the assessor may classify the task as simple and
        # never plan); subtasks carry workers in plan mode
        agg = {"tokens_in": 0, "tokens_out": 0, "model_calls": 0, "tool_calls": 0}
        verification_passed = outcome["status"] == "completed"
        failed_verifications = 0
        changed: set[str] = set()
        for sub in ledger.list_tasks(tid):
            for w in ledger.list_workers(sub["task_id"]):
                sid = w.get("session_id") or ""
                sdir = Path(cfg.sessions_dir) / sid
                if not (sdir / "events.jsonl").exists():
                    continue
                from jinlin_agent.state import recover as fold_events

                st = fold_events(__import__("jinlin_agent.events", fromlist=["read_events"]).read_events(sdir))
                sm = _session_metrics(st)
                for key in ("tokens_in", "tokens_out", "model_calls", "tool_calls"):
                    agg[key] += sm[key]
                changed.update(sm["changed_files"])
                if sm["changed_files"] and sm["verification_passed"] is False:
                    failed_verifications += 1
        integration_states = [r.get("integration", "") for r in ledger.list_tasks(tid) if r["task_id"] != tid]
        changed_tasks = [s for s in integration_states if s != "not_needed"]
        if manager is not None and changed_tasks:
            verification_passed = verification_passed and all(s == "integrated" for s in changed_tasks)
        verification_passed = verification_passed and failed_verifications == 0
        return {
            "status": outcome["status"],
            **agg,
            "changed_files": sorted(changed),
            "verification_passed": verification_passed,
            "human_interventions": _human_interventions(events),
            "replans": sum(1 for e in events if e.get("type") == "task.replanned"),
            "error_summary": outcome.get("reason") or outcome.get("summary") or "",
            "ledger_task": True,
            "subtask_integration": integration_states,
        }
    finally:
        runtime.shutdown()
        ledger.close()


def run_one(task: dict[str, Any], strategy: str, dry_run: bool, out_dir: Path) -> dict[str, Any]:
    workspace = _ensure_workspace(task, dry_run, out_dir)
    record: dict[str, Any] = {
        "task_id": task["id"],
        "strategy": strategy,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "not_run",
        "verification_passed": None,
        "changed_files": [],
        "wall_seconds": None,
        "tokens_in": 0,
        "tokens_out": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "replans": 0,
        "conflicts": 0,
        "human_interventions": 0,
        "error_summary": "",
    }
    t0 = time.monotonic()
    try:
        if strategy == "single":
            result = run_single(task, workspace, dry_run, out_dir)
        elif strategy == "planner":
            result = run_supervised(task, workspace, dry_run, "sequential", out_dir)
        elif strategy == "multi":
            result = run_supervised(task, workspace, dry_run, "multi", out_dir)
        else:
            raise SystemExit(f"unknown strategy {strategy}")
        for key in ("status", "tokens_in", "tokens_out", "model_calls", "tool_calls",
                    "changed_files", "verification_passed", "human_interventions",
                    "error_summary", "ledger_task", "subtask_integration"):
            if key in result:
                record[key] = result[key]
        record["replans"] = result.get("replans", 0)
        record["conflicts"] = 0  # conflicts surface as blocked; counted via events in future runs
    except Exception as exc:
        record["status"] = f"runner_crash: {type(exc).__name__}: {exc}"
        record["error_summary"] = f"{type(exc).__name__}: {exc}"
    record["wall_seconds"] = round(time.monotonic() - t0, 1)
    return record


def aggregate(records: list[dict[str, Any]]) -> dict[str, Any]:
    by_strategy: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_strategy.setdefault(record["strategy"], []).append(record)
    summary = {}
    for strategy, rows in by_strategy.items():
        total = len(rows)
        completed = sum(1 for r in rows if r["status"] == "completed")
        verified = sum(1 for r in rows if r["verification_passed"] is True)
        summary[strategy] = {
            "tasks": total,
            "success_rate": round(completed / total, 3) if total else None,
            "verification_pass_rate": round(verified / total, 3) if total else None,
            "avg_wall_seconds": round(sum(r["wall_seconds"] or 0 for r in rows) / total, 1) if total else None,
            "avg_tokens": round(sum((r["tokens_in"] or 0) + (r["tokens_out"] or 0) for r in rows) / total, 1) if total else None,
            "replans": sum(r["replans"] for r in rows),
            "human_interventions": sum(r["human_interventions"] for r in rows),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="WP-R10/F1-F3 benchmark matrix (real models unless --dry-run)")
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--out", default="reports/benchmark")
    parser.add_argument("--strategies", default="single,planner,multi")
    parser.add_argument("--dry-run", action="store_true", help="MockProvider through the REAL pipeline: real tasks, workers, gates — zero tokens")
    args = parser.parse_args()

    if not args.dry_run and not os.environ.get("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY is not set; use --dry-run for the zero-token real-pipeline run", file=sys.stderr)
        return 4

    tasks = load_tasks(Path(args.tasks))
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    records: list[dict[str, Any]] = []
    for task in tasks:
        for strategy in strategies:
            if strategy not in STRATEGIES:
                print(f"unknown strategy: {strategy}", file=sys.stderr)
                return 4
            print(f"[run] {task['id']} x {strategy} ...", flush=True)
            record = run_one(task, strategy, args.dry_run, out_dir)
            records.append(record)
            (out_dir / f"{task['id']}_{strategy}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(
                f"      -> status={record['status']} verification_passed={record['verification_passed']} "
                f"({record['wall_seconds']}s, tokens={record['tokens_in']}+{record['tokens_out']})",
                flush=True,
            )

    (out_dir / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(aggregate(records), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nrecords: {len(records)} -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
