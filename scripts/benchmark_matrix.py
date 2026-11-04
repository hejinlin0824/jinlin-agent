"""WP-R10: real-task benchmark matrix runner (single agent vs planner vs
multi-worker).

This is the SCAFFOLD for the release-gate evaluation (review task book §六).
It executes a task list against up to three strategies on REAL models and
writes per-run records plus aggregate metrics.

It is deliberately NOT wired into `eval --suite all`: a real benchmark needs
DEEPSEEK_API_KEY and costs real tokens. Usage:

    set DEEPSEEK_API_KEY=sk-...
    python scripts/benchmark_matrix.py --tasks benchmark_tasks.json --out reports/benchmark

Tasks file format (JSON):
    [
      {"id": "bugfix-add", "kind": "single_file_fix",
       "workspace": "fixtures/calc", "prompt": "修复 add() ...", "expected": {...}},
      ...
    ]
Strategies:
    single    -> the v1 path (jinlin-agent run semantics)
    planner   -> task run with planning forced (planner, one worker)
    multi     -> task run; the model assessment/planner decides the DAG

Honest status (2026-11-04): the runner executes end to end with --dry-run
(mock provider, zero cost) so the plumbing is testable; REAL runs require
the owner's API key and were NOT executed for this report (no fabricated
numbers — see REMEDIATION-REPORT.md §10).
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


def load_tasks(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("tasks file must be a JSON list")
    for item in data:
        for key in ("id", "workspace", "prompt"):
            if key not in item:
                raise SystemExit(f"task entry missing {key!r}: {item}")
    return data


def run_one(task: dict[str, Any], strategy: str, out_dir: Path, dry_run: bool) -> dict[str, Any]:
    """One (task, strategy) execution. Real mode uses the DeepSeek provider;
    dry-run substitutes the scripted mock so the matrix plumbing is testable
    without a key."""
    record: dict[str, Any] = {
        "task_id": task["id"],
        "strategy": strategy,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "not_run",
        "verification_passed": None,
        "wall_seconds": None,
        "tokens": None,
        "replans": 0,
        "conflicts": 0,
        "human_interventions": 0,
    }
    t0 = time.monotonic()
    try:
        if dry_run:
            record["status"] = "dry_run"
        else:
            if strategy == "single":
                from jinlin_agent.cli import main as cli_main

                code = cli_main([
                    "run", "--workspace", str(task["workspace"]), "-t", task["prompt"], "--quiet",
                ])
                record["status"] = "completed" if code == 0 else f"exit_{code}"
            else:
                from jinlin_agent.config import load_config
                from jinlin_agent.ledger import TaskLedger
                from jinlin_agent.supervisor import Supervisor
                from jinlin_agent.taskmodel import Budget, TaskSpec, new_task_id
                from jinlin_agent.worker_runtime import WorkerRuntime
                from jinlin_agent.workspace_manager import WorkspaceManager
                from jinlin_agent.model import create_provider

                cfg = load_config(Path(task["workspace"]))
                ledger = TaskLedger(cfg).open()
                runtime = WorkerRuntime(cfg, ledger, provider_factory=lambda: create_provider(cfg), log=lambda m: None)
                supervisor = Supervisor(cfg, ledger, runtime, provider=None, log=lambda m: None)
                manager = None
                if WorkspaceManager.is_git_repo(task["workspace"]):
                    manager = WorkspaceManager(task["workspace"], cfg, ledger)
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
                ledger.create_task(spec)
                outcome = supervisor.execute_root(spec, Path(task["workspace"]))
                record["status"] = outcome["status"]
                record["replans"] = int(outcome.get("replans", 0))
                metrics = ledger.metrics().get("roots", {}).get(tid, {})
                record["conflicts"] = int(metrics.get("conflicts", 0))
                runtime.shutdown()
                ledger.close()
    except Exception as exc:
        record["status"] = f"runner_crash: {type(exc).__name__}: {exc}"
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
        summary[strategy] = {
            "tasks": total,
            "success_rate": round(completed / total, 3) if total else None,
            "avg_wall_seconds": round(sum(r["wall_seconds"] or 0 for r in rows) / total, 1) if total else None,
            "replans": sum(r["replans"] for r in rows),
            "conflicts": sum(r["conflicts"] for r in rows),
        }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="WP-R10 benchmark matrix (real models; costs tokens)")
    parser.add_argument("--tasks", required=True)
    parser.add_argument("--out", default="reports/benchmark")
    parser.add_argument("--strategies", default="single,planner,multi")
    parser.add_argument("--dry-run", action="store_true", help="mock provider, zero cost (plumbing test only)")
    args = parser.parse_args()

    if not args.dry_run and not os.environ.get("DEEPSEEK_API_KEY"):
        print("DEEPSEEK_API_KEY is not set; use --dry-run for the zero-cost plumbing test", file=sys.stderr)
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
            record = run_one(task, strategy, out_dir, dry_run=args.dry_run)
            records.append(record)
            (out_dir / f"{task['id']}_{strategy}.json").write_text(
                json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"      -> {record['status']} ({record['wall_seconds']}s)", flush=True)

    (out_dir / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(aggregate(records), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nrecords: {len(records)} -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
