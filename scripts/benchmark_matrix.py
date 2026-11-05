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

WP-F3: every run records the full metric set (task_id, strategy, status,
verification_status/verification_passed/verification_evidence, changed_files,
wall_seconds, actual + estimated tokens, model_calls/attempts, tool_calls,
replans, conflicts, human_interventions, error_summary).

WP-C1 (third-round remediation): `verification_passed` comes ONLY from
verification run records evaluated against the profile's required levels
(`verify.verification_evidence`); empty records are `not_run` and therefore
False; `status` never participates; supervised roots aggregate worker +
integration evidence in layers.

WP-C2 (third-round remediation): provider usage (`tokens_*_actual`) and
local estimates (`tokens_estimated_context`) are separate fields; missing
provider usage is reported via `tokens_usage_missing_calls` /
`usage_complete` and `None` actuals — never a fake 0. `conflicts` is
`null` (not measured) until event-level counting exists.
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


def _session_metrics(
    state: Any,
    profile: str = "code",
    has_test_command: bool | None = None,
) -> dict[str, Any]:
    """Per-session metrics with WP-C1 verification semantics and WP-C2 token
    accounting.

    Verification: derived ONLY from verification run records against the
    profile's required levels (`verify.verification_evidence`). Empty run
    records are `not_run` -> `verification_passed is False`; session/task
    status never feeds this verdict.

    Tokens: provider-reported usage (`*_actual`) and local estimates
    (`tokens_estimated_context`) are separate; a session whose model calls
    returned no usage reports `None` actuals plus `usage_complete=False`,
    never a fake 0. `model_calls` counts model.requested events (logical
    calls); `model_attempts` counts model.attempt events, so one retry is
    one additional attempt."""
    from jinlin_agent.verify import verification_evidence

    changed = list(state.diff_paths())
    evidence = verification_evidence(
        getattr(state, "verification_runs", []) or [],
        profile=profile,
        has_test_command=has_test_command,
        changed=bool(changed),
    )
    attempts = int(getattr(state, "model_attempts", 0) or 0)
    missing = int(getattr(state, "tokens_usage_missing_calls", 0) or 0)
    usage_seen = attempts - missing
    if attempts == 0 or usage_seen == 0:
        tokens_in_actual = tokens_out_actual = tokens_total_actual = None
        usage_complete = False
    else:
        tokens_in_actual = int(getattr(state, "tokens_in_actual", 0) or 0)
        tokens_out_actual = int(getattr(state, "tokens_out_actual", 0) or 0)
        tokens_total_actual = tokens_in_actual + tokens_out_actual
        usage_complete = missing == 0
    return {
        "tokens_in_actual": tokens_in_actual,
        "tokens_out_actual": tokens_out_actual,
        "tokens_total_actual": tokens_total_actual,
        "tokens_estimated_context": int(getattr(state, "tokens_estimated_context", 0) or 0),
        "tokens_usage_missing_calls": missing,
        "usage_complete": usage_complete,
        "model_calls": int(getattr(state, "model_calls", 0) or 0),
        "model_attempts": attempts,
        "tool_calls": int(getattr(state, "tool_calls_total", 0) or 0),
        "changed_files": changed,
        "verification_status": evidence["status"],
        "verification_passed": evidence["passed"],
        "verification_evidence": evidence,
    }


def _aggregate_verification(components: list[dict[str, Any]]) -> dict[str, Any]:
    """Layered aggregation (WP-C1 rule 6): the root verdict is derived from
    every component's independent evidence — one worker's pass never upgrades
    a failed or missing sibling, and status never participates."""
    statuses = [c.get("verification_status") for c in components]
    if any(s == "failed" for s in statuses):
        status = "failed"
    elif any(s == "error" for s in statuses):
        # WP-R4-02: an error component (e.g. unknown verification profile)
        # propagates as error — never overridden by a sibling's pass
        status = "error"
    elif any(s == "not_run" for s in statuses):
        status = "not_run"
    elif any(s == "passed" for s in statuses):
        status = "passed"
    else:
        status = "not_applicable"
    merged: dict[str, Any] = {
        "status": status,
        "passed": status == "passed",
        "required_levels": sorted({lv for c in components for lv in c.get("verification_evidence", {}).get("required_levels", [])}),
        "executed_levels": sorted({lv for c in components for lv in c.get("verification_evidence", {}).get("executed_levels", [])}),
        "failed_levels": sorted({lv for c in components for lv in c.get("verification_evidence", {}).get("failed_levels", [])}),
        "skipped_levels": sorted({lv for c in components for lv in c.get("verification_evidence", {}).get("skipped_levels", [])}),
        "notes": [f"[{i}] {n}" for i, c in enumerate(components) for n in c.get("verification_evidence", {}).get("notes", [])],
        "components": len(components),
    }
    return {"verification_status": status, "verification_passed": status == "passed", "verification_evidence": merged}


def _integration_evidence(events: list[dict[str, Any]], required: bool) -> dict[str, Any]:
    """Integration-phase verification component (WP-C1 rule 6): required when
    at least one subtask carries changes (`integration=pending`); its verdict
    comes only from `integration.verified` records, never from the merged
    commit's existence."""
    verdicts = [e for e in events if e.get("type") == "integration.verified"]
    if not required:
        return {
            "verification_status": "not_applicable",
            "verification_passed": False,
            "verification_evidence": {
                "status": "not_applicable",
                "required_levels": [],
                "executed_levels": [],
                "failed_levels": [],
                "skipped_levels": [],
                "notes": ["no subtask delivered changes; integration verification not applicable"],
                "profile": "integration",
            },
        }
    if not verdicts:
        return {
            "verification_status": "not_run",
            "verification_passed": False,
            "verification_evidence": {
                "status": "not_run",
                "required_levels": ["integration-verify"],
                "executed_levels": [],
                "failed_levels": [],
                "skipped_levels": [],
                "notes": ["integration required but no integration.verified record"],
                "profile": "integration",
            },
        }
    if any(not e.get("payload", {}).get("passed", False) for e in verdicts):
        status = "failed"
    else:
        status = "passed"
    return {
        "verification_status": status,
        "verification_passed": status == "passed",
        "verification_evidence": {
            "status": status,
            "required_levels": ["integration-verify"],
            "executed_levels": ["integration-verify"] if status == "passed" else [],
            "failed_levels": [] if status == "passed" else ["integration-verify"],
            "skipped_levels": [],
            "notes": [],
            "profile": "integration",
        },
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
    from jinlin_agent.verify import detect_project

    # WP-C1 rule 8: single has no TaskSpec — the verification verdict is
    # rebuilt from the SESSION event log against the default 'code' profile;
    # a session without verification events reports not_run / False.
    has_tests = bool(detect_project(workspace, cfg).test_commands)
    metrics = _session_metrics(state, profile="code", has_test_command=has_tests)
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
        fields = root_record_fields(ledger, cfg, tid, workspace, events)
        fields["status"] = outcome["status"]
        fields["error_summary"] = outcome.get("reason") or outcome.get("summary") or ""
        return {
            **fields,
            "ledger_task": True,
        }
    finally:
        runtime.shutdown()
        ledger.close()


def root_record_fields(
    ledger: Any, cfg: Any, root_id: str, workspace: Path, events: list[dict[str, Any]],
) -> dict[str, Any]:
    """Aggregate one supervised root's record fields from the ledger, the
    worker session event logs and the root task event log.

    Shared by the live runner AND the WP-C2 recompute script, so recomputed
    records and freshly produced records come from ONE code path. WP-C1: the
    root's verification verdict is layered aggregation over every worker's
    independent evidence plus the integration phase — outcome status never
    initializes it. Workers attached directly to the root (direct mode) are
    included; subtasks carry workers in plan mode.

    WP-R4-01 token semantics:
    * actual values are summed ONLY from observed provider usage — a worker
      whose session has no usage contributes ``None``-ness, never a 0;
    * ``tokens_total_actual`` is computed at root level and is ``null``
      unless at least one usage observation exists;
    * ``usage_complete`` is true only when every expected worker session was
      read AND every recorded attempt carried usage — a missing session log
      fails completeness and is counted in ``usage_missing_sessions``
      (the attempt count of an unreadable session is unknowable, so it is
      NOT folded into tokens_usage_missing_calls)."""
    from jinlin_agent.verify import detect_project

    has_test_command = bool(detect_project(workspace, cfg).test_commands)
    agg = {
        "tokens_estimated_context": 0,
        "tokens_usage_missing_calls": 0,
        "model_calls": 0,
        "model_attempts": 0,
        "tool_calls": 0,
    }
    actual_in: int | None = None
    actual_out: int | None = None
    components: list[dict[str, Any]] = []
    changed: set[str] = set()
    worker_metrics: list[dict[str, Any]] = []
    expected_workers = 0
    missing_sessions = 0
    subtasks = [r for r in ledger.list_tasks(root_id) if r["task_id"] != root_id]
    for sub in ledger.list_tasks(root_id):
        profile = (sub.get("verification") or "code").strip().lower()
        for w in ledger.list_workers(sub["task_id"]):
            expected_workers += 1
            sid = w.get("session_id") or ""
            sdir = Path(cfg.sessions_dir) / sid
            if not (sdir / "events.jsonl").exists():
                missing_sessions += 1
                components.append(_missing_session_component(sid, profile, has_test_command))
                continue
            from jinlin_agent.state import recover as fold_events

            st = fold_events(__import__("jinlin_agent.events", fromlist=["read_events"]).read_events(sdir))
            sm = _session_metrics(st, profile=profile, has_test_command=has_test_command)
            worker_metrics.append(sm)
            for key in agg:
                agg[key] += sm[key] or 0
            if sm["tokens_in_actual"] is not None:
                actual_in = (actual_in or 0) + sm["tokens_in_actual"]
            if sm["tokens_out_actual"] is not None:
                actual_out = (actual_out or 0) + sm["tokens_out_actual"]
            changed.update(sm["changed_files"])
            components.append(sm)
    integration_states = [r.get("integration", "") for r in subtasks]
    integration_required = any(s == "pending" for s in integration_states)
    components.append(_integration_evidence(events, required=integration_required))
    root_verification = _aggregate_verification(components)
    usage_observed = actual_in is not None or actual_out is not None
    usage_complete = (
        expected_workers > 0
        and missing_sessions == 0
        and bool(worker_metrics)
        and all(m["usage_complete"] for m in worker_metrics)
    )
    return {
        "tokens_in_actual": actual_in if usage_observed else None,
        "tokens_out_actual": actual_out if usage_observed else None,
        "tokens_total_actual": (actual_in + actual_out) if usage_observed else None,
        **agg,
        # usage is complete only when every expected worker session was read
        # and every recorded model attempt carried provider usage
        "usage_complete": usage_complete,
        "usage_missing_sessions": missing_sessions,
        "usage_expected_sessions": expected_workers,
        "changed_files": sorted(changed),
        **root_verification,
        "human_interventions": _human_interventions(events),
        "replans": sum(1 for e in events if e.get("type") == "task.replanned"),
        "subtask_integration": integration_states,
    }


def _missing_session_component(sid: str, profile: str, has_test_command: bool | None) -> dict[str, Any]:
    """A worker with no session event log contributes `not_run` evidence —
    a missing record is never a pass (WP-C1 rule 3)."""
    from jinlin_agent.verify import verification_evidence

    ev = verification_evidence([], profile=profile, has_test_command=has_test_command, changed=False)
    ev["notes"].append(f"worker session {sid or '<unbound>'} has no event log")
    return {"verification_status": ev["status"], "verification_passed": False, "verification_evidence": ev}


def run_one(task: dict[str, Any], strategy: str, dry_run: bool, out_dir: Path) -> dict[str, Any]:
    workspace = _ensure_workspace(task, dry_run, out_dir)
    record: dict[str, Any] = {
        "task_id": task["id"],
        "strategy": strategy,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "status": "not_run",
        "verification_status": None,
        "verification_passed": None,
        "verification_evidence": None,
        "changed_files": [],
        "wall_seconds": None,
        "tokens_in_actual": None,
        "tokens_out_actual": None,
        "tokens_total_actual": None,
        "tokens_estimated_context": 0,
        "tokens_usage_missing_calls": 0,
        "usage_missing_sessions": 0,
        "usage_expected_sessions": 1,
        "usage_complete": False,
        "model_calls": 0,
        "model_attempts": 0,
        "tool_calls": 0,
        "replans": 0,
        "conflicts": None,  # not measured yet — never reported as a fake 0
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
        for key in ("status", "tokens_in_actual", "tokens_out_actual", "tokens_total_actual",
                    "tokens_estimated_context", "tokens_usage_missing_calls", "usage_missing_sessions",
                    "usage_expected_sessions", "usage_complete",
                    "model_calls", "model_attempts", "tool_calls", "changed_files",
                    "verification_status", "verification_passed", "verification_evidence",
                    "human_interventions", "error_summary", "ledger_task", "subtask_integration"):
            if key in result:
                record[key] = result[key]
        record["replans"] = result.get("replans", 0)
        # conflicts surface as blocked tasks; event-level counting is not
        # implemented yet, so this stays explicitly unmeasured (never 0)
        record["conflicts"] = None
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
        # WP-R4-01: distinguish null (no provider usage observed at all) from
        # a legitimate numeric 0. A run counts as "with usage" when at least
        # one provider usage observation exists — not when a token number is
        # merely non-zero.
        with_usage = [
            r for r in rows
            if r.get("tokens_in_actual") is not None or r.get("tokens_out_actual") is not None
        ]
        complete_runs = [r for r in with_usage if r.get("usage_complete") is True]
        partial_runs = [r for r in with_usage if r.get("usage_complete") is not True]
        totals = [
            r.get("tokens_total_actual") for r in with_usage
            if r.get("tokens_total_actual") is not None
        ]
        summary[strategy] = {
            "tasks": total,
            "success_rate": round(completed / total, 3) if total else None,
            "verification_pass_rate": round(verified / total, 3) if total else None,
            "avg_wall_seconds": round(sum(r["wall_seconds"] or 0 for r in rows) / total, 1) if total else None,
            "tokens_in_actual_total": sum(r["tokens_in_actual"] for r in with_usage if r["tokens_in_actual"] is not None) if with_usage else None,
            "tokens_out_actual_total": sum(r["tokens_out_actual"] for r in with_usage if r["tokens_out_actual"] is not None) if with_usage else None,
            "tokens_total_actual_total": sum(totals) if totals else None,
            "tokens_estimated_context_total": sum(r.get("tokens_estimated_context") or 0 for r in rows),
            "runs_with_usage": len(with_usage),
            "usage_complete_runs": len(complete_runs),
            "runs_with_partial_usage": len(partial_runs),
            "runs_without_usage": total - len(with_usage),
            "model_calls": sum(r.get("model_calls") or 0 for r in rows),
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
                f"      -> status={record['status']} verification_status={record.get('verification_status')} "
                f"({record['wall_seconds']}s, tokens_actual={record.get('tokens_in_actual')}+{record.get('tokens_out_actual')}, "
                f"usage_complete={record.get('usage_complete')})",
                flush=True,
            )

    (out_dir / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(aggregate(records), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nrecords: {len(records)} -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
