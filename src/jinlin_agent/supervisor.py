"""Supervisor (v2.0 WP-V2.0-3; ADR-002, ADR-009).

Global control for one root task: assess the strategy, plan only when it pays,
schedule the subtask DAG respecting dependencies, replan within an explicit
budget, and stop when the budget is exhausted. The Supervisor manages tasks —
it is not an unbounded universal executor (v2 §4.2), and it never executes a
worker's job itself.

Strategy selection (v2 §5.1) is a hybrid:
  * with a provider: one structured model assessment (ASSESSOR_SYSTEM);
  * without a provider (and as fallback when the model answer is unusable):
    a narrow deterministic rule set whose default is DIRECT — simple tasks
    never plan and never create subtasks (Single Agent First).

Scheduling loop invariants:
  * a subtask starts only when ALL its dependencies are completed;
  * a failed subtask triggers at most `runtime.max_replans` local replans;
    each replan supersedes the failed node's non-terminal downstream
    (cancelled as stale) and registers replacement subtasks — completed work
    is never silently discarded or re-run;
  * a waiting subtask surfaces to the caller (worker_id included);
    execute_root is resumable: re-enter it after resume and the schedule
    continues from ledger state (planning is not repeated).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import AgentError
from .ledger import TaskLedger
from .planner import ASSESSOR_SYSTEM, complete_json, plan_subtasks
from .taskmodel import TaskSpec, validate_dependency_graph

STRATEGIES = (
    "direct",
    "plan_then_execute",
    "parallel_workers",
    "sequential_workers",
    "human_input_required",
    "refuse_or_stop",
)

_NON_TERMINAL_TASK_STATUSES = {"created", "queued", "running", "waiting_user", "waiting_approval", "blocked"}


# --------------------------------------------------------------------------- objective scan (v2 §5.1)


def scan_workspace(workspace: Path | str, config: Any, sandbox: Any = None) -> dict[str, Any]:
    """Cheap, bounded, deterministic repository signals. Model judgement is
    layered on top of these — never instead of them. Repository size is a
    signal, never the verdict (v2 §5.2)."""
    root = Path(workspace)
    ignore = set(config.project.get("ignore_dirs", [])) or {".git", "node_modules", ".venv", "__pycache__"}
    ext_counts: dict[str, int] = {}
    file_count = 0
    large_binaries = 0
    has_tests = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ignore]
        for name in filenames:
            file_count += 1
            rel = Path(dirpath) / name
            suffix = rel.suffix.lower().lstrip(".")
            if suffix:
                ext_counts[suffix] = ext_counts.get(suffix, 0) + 1
            if name.startswith("test_") or name.endswith("_test.py") or Path(dirpath).name in ("tests", "test"):
                has_tests = True
            try:
                if rel.stat().st_size > 1_048_576:
                    large_binaries += 1
            except OSError:
                pass
            if file_count >= 2000:
                break
        if file_count >= 2000:
            break
    _LANG = {
        "py": "python", "js": "node", "ts": "node", "rs": "rust", "go": "go",
        "java": "java", "c": "c", "h": "c", "cpp": "cpp", "cs": "csharp", "rb": "ruby", "php": "php",
    }
    languages = [lang for ext, lang in _LANG.items() if ext_counts.get(ext)]
    snapshot: dict[str, Any] = {
        "file_count": file_count,
        "languages": languages,
        "has_tests": has_tests,
        "large_binaries": large_binaries,
        "top_extensions": sorted(ext_counts.items(), key=lambda kv: -kv[1])[:5],
        "git_repo": False,
        "git_dirty": 0,
    }
    if sandbox is not None:
        res = sandbox.run(["git", "status", "--porcelain=v1"], str(root), timeout=30)
        if res.exit_code == 0:
            lines = [l for l in (res.stdout or "").splitlines() if l.strip()]
            snapshot["git_repo"] = True
            snapshot["git_dirty"] = len(lines)
    return snapshot


# --------------------------------------------------------------------------- assessment (v2 §5.1)


@dataclass
class Assessment:
    strategy: str
    reasons: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)


def _assess_deterministic(request: str) -> Assessment:
    """Narrow, predictable fallback. Default is DIRECT (ADR-002): only an
    explicitly structured or explicitly parallel/sequential request plans."""
    numbered = len(re.findall(r"(?m)^\s*\d+[).、]", request)) >= 2
    parallel = bool(re.search(r"(并行|同时进行|分别处理|in parallel|simultaneously)", request, re.IGNORECASE))
    sequential = bool(re.search(r"(然后|之后|再|然后最后|then|after that|afterwards)", request, re.IGNORECASE))
    if parallel:
        return Assessment("parallel_workers", ["explicit parallel intent in the request"])
    if numbered:
        return Assessment("plan_then_execute", ["request enumerates numbered steps"])
    if sequential:
        return Assessment("sequential_workers", ["explicit sequential intent in the request"])
    return Assessment("direct", ["no decomposition markers; default to direct execution"])


def assess_complexity(request: str, scan: dict[str, Any], provider: Any = None) -> Assessment:
    """Hybrid assessment: model judgement when a provider is available, the
    deterministic rule set otherwise or when the model answer is unusable."""
    fallback_reasons: list[str] = []
    if provider is not None:
        try:
            data = complete_json(
                provider,
                ASSESSOR_SYSTEM,
                "Request:\n" + request + "\n\nRepository signals:\n" + json.dumps(scan, ensure_ascii=False),
            )
            strategy = str(data.get("strategy", "")).strip()
            if strategy in STRATEGIES:
                return Assessment(strategy, [str(r) for r in data.get("reasons") or []], scan)
            fallback_reasons.append(f"model returned unknown strategy {strategy!r}")
        except AgentError as exc:
            fallback_reasons.append(f"model assessment failed ({exc.code})")
    fallback_reasons.append("deterministic assessment")
    det = _assess_deterministic(request)
    return Assessment(det.strategy, fallback_reasons + det.reasons, scan)


# --------------------------------------------------------------------------- supervisor


class Supervisor:
    def __init__(
        self,
        config: Any,
        ledger: TaskLedger,
        runtime: Any,
        provider: Any = None,
        log: Any = None,
    ) -> None:
        self.config = config
        self.ledger = ledger
        self.runtime = runtime
        self.provider = provider
        self.log = log or (lambda msg: None)
        self.workspace_manager: Any | None = None

    def attach_workspace_manager(self, manager: Any) -> None:
        """WP-V2.0-5: give every WRITE worker its own git worktree and run the
        Integration Coordinator before the root completes. Without a manager
        the supervisor behaves exactly as before (shared workspace)."""
        self.workspace_manager = manager

    # ------------------------------------------------------------------ entry

    def execute_root(self, root_spec: TaskSpec, workspace: Path | str) -> dict[str, Any]:
        root_id = root_spec.task_id
        root_row = self.ledger.require_task(root_id)
        if root_row["status"] in ("completed", "failed", "cancelled"):
            return {"status": root_row["status"], "task_id": root_id, "reason": "root already terminal"}

        subtasks = [t for t in self.ledger.list_tasks(root_id) if t["task_id"] != root_id]
        if root_row["status"] == "created" and not subtasks:
            request = self._request_text(root_spec)
            scan = scan_workspace(workspace, self.config)
            assessment = assess_complexity(request, scan, self.provider)
            self.ledger.emit(
                root_id,
                "task.strategy_selected",
                {"root_task_id": root_id, "strategy": assessment.strategy, "reasons": assessment.reasons, "signals": scan},
            )
            if assessment.strategy == "direct":
                return self.runtime.run_task(root_id, workspace)
            if assessment.strategy == "human_input_required":
                self._advance_task(root_id, "waiting_user", "; ".join(assessment.reasons))
                return {"status": "waiting_user", "task_id": root_id, "reasons": assessment.reasons}
            if assessment.strategy == "refuse_or_stop":
                self._advance_task(root_id, "failed", "; ".join(assessment.reasons))
                return {"status": "failed", "task_id": root_id, "reasons": assessment.reasons}
            # planning strategies
            self._advance_task(root_id, "running", f"planning ({assessment.strategy})")
            try:
                self._register_plan(root_spec, request, scan)
            except AgentError as exc:
                self._advance_task(root_id, "failed", f"planning failed: {exc.message}")
                return {"status": "failed", "task_id": root_id, "reason": exc.message}
        return self._schedule(root_spec, workspace)

    # ------------------------------------------------------------------ planning

    def _register_plan(self, root_spec: TaskSpec, request: str, scan: dict[str, Any]) -> list[TaskSpec]:
        specs = plan_subtasks(
            self.provider,
            goal=request,
            context=json.dumps(scan, ensure_ascii=False),
            root_id=root_spec.task_id,
        )
        for spec in specs:
            self.ledger.create_task(spec)
            self.ledger.set_task_status(spec.task_id, "queued", reason="planned")
        return specs

    # ------------------------------------------------------------------ scheduling

    def _schedule(self, root_spec: TaskSpec, workspace: Path | str) -> dict[str, Any]:
        root_id = root_spec.task_id
        max_replans = max(0, int(self.config.runtime.get("max_replans", 3)))
        replans = 0
        superseded: set[str] = set()
        while True:
            rows = [t for t in self.ledger.list_tasks(root_id) if t["task_id"] != root_id]
            completed = {r["task_id"] for r in rows if r["status"] == "completed"}
            active_failed = [r for r in rows if r["status"] == "failed" and r["task_id"] not in superseded]
            pending = [r for r in rows if r["status"] in _NON_TERMINAL_TASK_STATUSES]

            if active_failed:
                if replans >= max_replans:
                    reason = f"replan budget exhausted ({replans}/{max_replans})"
                    self._advance_task(root_id, "failed", reason)
                    return {"status": "failed", "task_id": root_id, "reason": reason, "replans": replans}
                replans += 1
                self.ledger.emit(
                    root_id,
                    "task.replanned",
                    {"root_task_id": root_id, "round": replans, "failed": [f["task_id"] for f in active_failed]},
                )
                self._replan(root_spec, active_failed, rows, superseded)
                continue

            runnable = [
                r for r in rows
                if r["status"] == "queued" and all(dep in completed for dep in r["dependencies"])
            ]
            if runnable:
                submitted: list[tuple[str, str]] = []
                for r in runnable:
                    target_workspace = workspace
                    branch = ""
                    base_commit = ""
                    if self.workspace_manager is not None:
                        ws = self.workspace_manager.create_worker_workspace(r["task_id"])
                        target_workspace = ws.path
                        branch = ws.branch
                        base_commit = ws.base_commit
                    worker_id = self.runtime.submit_task(
                        r["task_id"], target_workspace, branch=branch, base_commit=base_commit
                    )
                    submitted.append((r["task_id"], worker_id))
                for task_id, worker_id in submitted:
                    out = self.runtime.wait_worker(worker_id)
                    if out["status"] == "failed":
                        # covers both session failure and the WP-V2.0-8
                        # verification gate (the worker runtime owns that
                        # decision — it knows the project's test commands)
                        self._advance_task(task_id, "failed", reason=out.get("summary") or "attempt failed")
                    if out["status"] == "review_pending":
                        row = self.ledger.get_task(task_id)
                        if row and row.get("requires_review"):
                            accepted, actions = self._review_completed_task(row)
                            if accepted:
                                self._advance_task(task_id, "completed", reason="reviewer accepted the deliverable")
                            else:
                                self._advance_task(
                                    task_id,
                                    "failed",
                                    reason="reviewer rejected the deliverable: " + "; ".join(actions),
                                )
                    if out["status"] in ("waiting_user", "waiting_approval"):
                        return {
                            "status": out["status"],
                            "task_id": task_id,
                            "worker_id": worker_id,
                            "replans": replans,
                        }
                continue

            if not pending:
                # resolved = completed, or superseded by a bounded replan
                # (the replaced node's own failed/cancelled row stays as evidence)
                resolved = {r["task_id"] for r in rows if r["status"] == "completed" or r["task_id"] in superseded}
                if rows and all(r["task_id"] in resolved for r in rows):
                    if self.workspace_manager is not None:
                        integration = self._integrate_all(root_id)
                        if integration["status"] == "conflict":
                            # v2 §5.4: worker result conflicts pause the
                            # integration and ask for adjudication
                            self._advance_task(root_id, "blocked", reason="integration conflict needs adjudication")
                            return {
                                "status": "blocked",
                                "task_id": root_id,
                                "conflicts": integration.get("conflicts", []),
                                "replans": replans,
                            }
                        if integration["status"] != "integrated":
                            reason = f"integration failed: {integration['status']}"
                            self._advance_task(root_id, "failed", reason=reason)
                            return {"status": "failed", "task_id": root_id, "reason": reason, "replans": replans}
                    self._advance_task(root_id, "completed", "all subtasks resolved")
                    return {"status": "completed", "task_id": root_id, "replans": replans}
                self._advance_task(root_id, "failed", "no runnable subtasks and the set is not fully resolved")
                return {"status": "failed", "task_id": root_id, "reason": "unsatisfiable task set", "replans": replans}

            waiting = [r for r in pending if r["status"] in ("waiting_user", "waiting_approval")]
            if waiting:
                victim = waiting[0]
                workers = [w for w in self.ledger.list_workers(victim["task_id"]) if w["status"] == victim["status"]]
                return {
                    "status": victim["status"],
                    "task_id": victim["task_id"],
                    "worker_id": workers[-1]["worker_id"] if workers else "",
                    "replans": replans,
                }
            # pending but nothing runnable and nobody waiting: dependencies can
            # never be satisfied (cancelled/failed upstream that was never replaced)
            self._advance_task(root_id, "failed", "queued subtasks have unsatisfiable dependencies")
            return {"status": "failed", "task_id": root_id, "reason": "unsatisfiable dependencies", "replans": replans}

    # ------------------------------------------------------------------ replanning

    def _replan(self, root_spec: TaskSpec, failed_rows: list[dict[str, Any]], rows: list[dict[str, Any]], superseded: set[str]) -> None:
        failed_ids = {f["task_id"] for f in failed_rows}
        # transitive dependents of the failed set are stale
        downstream: set[str] = set()
        changed = True
        while changed:
            changed = False
            for r in rows:
                if r["task_id"] in failed_ids | downstream or r["task_id"] in downstream:
                    continue
                if any(dep in failed_ids | downstream for dep in r["dependencies"]):
                    downstream.add(r["task_id"])
                    changed = True
        for r in rows:
            if r["task_id"] in downstream and r["status"] in _NON_TERMINAL_TASK_STATUSES:
                self.ledger.set_task_status(r["task_id"], "cancelled", reason="stale: upstream replaced by replan")
        superseded.update(failed_ids)
        superseded.update(downstream)

        failed_summary = [
            {"task_id": f["task_id"], "title": f["title"], "error": f.get("error") or ""} for f in failed_rows
        ]
        remaining = [r for r in rows if r["task_id"] in downstream and r["status"] == "completed"]
        instruction = (
            "REPLANNING (bounded): the following subtasks FAILED; propose replacement subtasks that cover "
            "their goals AND the goals of their stale dependents. Completed subtasks must NOT be redone: "
            + json.dumps(failed_summary, ensure_ascii=False)
            + " Already-completed (do not re-plan): "
            + json.dumps([r["title"] for r in remaining], ensure_ascii=False)
            + " Superseded (cancelled) titles: "
            + json.dumps(sorted({r["title"] for r in rows if r["task_id"] in downstream}), ensure_ascii=False)
        )
        specs = plan_subtasks(
            self.provider,
            goal=self._request_text(root_spec),
            context=json.dumps({"failed": failed_summary}, ensure_ascii=False),
            root_id=root_spec.task_id,
            instruction=instruction,
        )
        errors = validate_dependency_graph(specs)
        if errors:
            raise AgentError("MODEL_INVALID_RESPONSE", "replan produced an invalid DAG: " + "; ".join(errors), source="supervisor")
        for spec in specs:
            self.ledger.create_task(spec)
            self.ledger.set_task_status(spec.task_id, "queued", reason="replanned")

    # ------------------------------------------------------------------ integration (WP-V2.0-5)

    def _integrate_all(self, root_id: str) -> dict[str, Any]:
        """Merge every completed worker's worktree into the integration branch,
        attach the commits to the task results, release the worker worktrees.
        The integration worktree itself is kept as the deliverable staging
        until the owner merges/releases."""
        assert self.workspace_manager is not None
        conflicts: list[dict[str, Any]] = []
        for row in self.ledger.list_tasks(root_id):
            if row["task_id"] == root_id or row["status"] != "completed":
                continue
            if self.workspace_manager.get(row["task_id"]) is None:
                continue  # task ran without a worktree (read-only replan leftover)
            outcome = self.workspace_manager.integrate(row["task_id"], root_id)
            if outcome["status"] == "conflict":
                conflicts.append({"task_id": row["task_id"], "conflict_files": outcome.get("conflict_files", [])})
                continue
            if outcome["status"] != "integrated":
                return {"status": outcome["status"], "task_id": row["task_id"], "detail": outcome}
            if outcome.get("commit") and row.get("result"):
                self.ledger.attach_commit(row["task_id"], outcome["commit"])
            self.workspace_manager.cleanup_worker_workspace(row["task_id"])
        if conflicts:
            return {"status": "conflict", "conflicts": conflicts}
        return {"status": "integrated"}

    # ------------------------------------------------------------------ review gate (WP-V2.0-4)

    def _review_completed_task(self, row: dict[str, Any]) -> tuple[bool, list[str]]:
        """Independent reviewer over the structured deliverable (v2 §12.4).
        The request, verdict and outcome are protocol messages, not prose."""
        from .protocol import conduct_review, record_review, request_review
        from .ledger import task_spec_from_row

        spec = task_spec_from_row(row)
        workers = [w for w in self.ledger.list_workers(row["task_id"]) if w["status"] == "completed"]
        worker_id = workers[-1]["worker_id"] if workers else ""
        request_review(self.ledger, spec, worker_id=worker_id)
        result = row.get("result") or {}
        evidence = "; ".join(
            f"{t.get('command', '')} -> {t.get('status', '')}" for t in result.get("tests", []) if isinstance(t, dict)
        )
        review = conduct_review(self.provider, row["title"], result, evidence)
        record_review(self.ledger, spec, worker_id=worker_id, review=review)
        self.log(f"[supervisor] review of {row['task_id']}: {review.status}")
        return review.status == "accepted", review.required_actions

    # ------------------------------------------------------------------ helpers

    def _advance_task(self, task_id: str, new_status: str, reason: str = "") -> None:
        row = self.ledger.get_task(task_id)
        if row is None:
            return
        from .taskmodel import can_transition

        if can_transition(row["status"], new_status):
            self.ledger.set_task_status(task_id, new_status, reason=reason)

    def _request_text(self, spec: TaskSpec) -> str:
        lines = [spec.title or spec.task_id]
        if spec.description.strip():
            lines.append("")
            lines.append(spec.description.strip())
        if spec.acceptance_criteria:
            lines.append("")
            lines.append("Acceptance criteria:")
            lines.extend(f"- {c}" for c in spec.acceptance_criteria)
        return "\n".join(lines)
