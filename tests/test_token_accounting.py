"""WP-C2 regression: provider usage and local estimates are separate.

`tokens_*_actual` sums ONLY model.attempt provider usage;
`tokens_estimated_context` sums ONLY context.built estimates; a model
attempt without usage is counted in `tokens_usage_missing_calls` and the
benchmark reports None actuals with usage_complete=False — never a fake 0.
The legacy mixed `tokens_in/out` counters stay on SessionState for the
budget gate and are asserted here to remain untouched.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from benchmark_matrix import (  # noqa: E402
    _ASSESS_PLAN,
    _DRY_PLAN,
    _DRY_RUN_SCRIPT,
    _session_metrics,
    aggregate,
    run_one,
)

from jinlin_agent.state import recover as fold_events  # noqa: E402


TASK = {"id": "analyze-demo", "prompt": "analyze the entry point"}


def _event(etype: str, payload: dict, seq: int) -> dict:
    return {"type": etype, "payload": payload, "seq": seq}


def _usage_events(*, estimate: int = 100, prompt: int = 80, completion: int = 20, attempts: int = 1) -> list[dict]:
    evs = [_event("context.built", {"tokens_estimate": estimate}, 1)]
    for i in range(attempts):
        evs.append(_event("model.attempt", {"usage": {"prompt_tokens": prompt, "completion_tokens": completion}}, 2 + i))
    return evs


def test_actual_usage_excludes_context_estimates():
    state = fold_events(_usage_events())
    assert state.tokens_in_actual == 80
    assert state.tokens_out_actual == 20
    assert state.tokens_estimated_context == 100


def test_estimated_context_tokens_are_reported_separately():
    state = fold_events(_usage_events())
    metrics = _session_metrics(state)
    assert metrics["tokens_in_actual"] == 80
    assert metrics["tokens_estimated_context"] == 100
    assert metrics["tokens_total_actual"] == 100  # 80 + 20, NOT 80 + 100


def test_missing_provider_usage_is_not_reported_as_zero_actual():
    state = fold_events([_event("model.attempt", {}, 1)])
    metrics = _session_metrics(state)
    assert metrics["tokens_in_actual"] is None
    assert metrics["tokens_out_actual"] is None
    assert metrics["tokens_total_actual"] is None
    assert metrics["usage_complete"] is False
    assert metrics["tokens_usage_missing_calls"] == 1


def test_mock_provider_never_claims_actual_usage(tmp_path):
    """A dry-run session drives MockProvider: no attempt returns usage, so the
    record must show None actuals and usage_complete=False (never 0)."""
    record = run_one(TASK, "single", dry_run=True, out_dir=tmp_path)
    assert record["tokens_in_actual"] is None
    assert record["tokens_out_actual"] is None
    assert record["usage_complete"] is False
    assert record["tokens_usage_missing_calls"] >= 1


def test_retry_usage_is_counted_once_per_recorded_attempt():
    """Counting rule: each RECORDED model attempt contributes its own usage
    exactly once; one retry is one additional model_attempts entry."""
    state = fold_events(_usage_events(attempts=3))
    metrics = _session_metrics(state)
    assert state.model_attempts == 3
    assert metrics["model_attempts"] == 3
    assert metrics["tokens_in_actual"] == 3 * 80
    assert metrics["tokens_out_actual"] == 3 * 20


def test_partial_usage_reports_missing_calls():
    evs = [
        _event("model.attempt", {"usage": {"prompt_tokens": 80, "completion_tokens": 20}}, 1),
        _event("model.attempt", {}, 2),
    ]
    metrics = _session_metrics(fold_events(evs))
    assert metrics["usage_complete"] is False
    assert metrics["tokens_usage_missing_calls"] == 1
    assert metrics["tokens_in_actual"] == 80  # the seen usage is still summed


def test_benchmark_summary_sums_actual_usage_from_runs():
    rows = [
        {
            "strategy": "single", "status": "completed", "verification_passed": True,
            "wall_seconds": 1.0, "tokens_in_actual": 100, "tokens_out_actual": 10,
            "tokens_estimated_context": 5, "usage_complete": True, "model_calls": 1,
            "replans": 0, "human_interventions": 0,
        },
        {
            "strategy": "single", "status": "completed", "verification_passed": False,
            "wall_seconds": 2.0, "tokens_in_actual": None, "tokens_out_actual": None,
            "tokens_estimated_context": 7, "usage_complete": False, "model_calls": 1,
            "replans": 0, "human_interventions": 0,
        },
    ]
    summary = aggregate(rows)
    assert summary["single"]["tokens_in_actual_total"] == 100  # None run not folded as 0
    assert summary["single"]["tokens_out_actual_total"] == 10
    assert summary["single"]["runs_with_usage"] == 1
    assert summary["single"]["usage_complete_runs"] == 1
    assert summary["single"]["tokens_estimated_context_total"] == 12


def test_event_recomputed_usage_matches_run_record():
    """The mandated synthetic sample: estimate 100, usage 80/20 ->
    actual 80/20/100 and estimated 100 — recomputed from raw events, the
    same path the recompute script uses for the committed event streams."""
    state = fold_events(_usage_events())
    metrics = _session_metrics(state)
    assert metrics == {
        **{k: v for k, v in metrics.items()},
        "tokens_in_actual": 80,
        "tokens_out_actual": 20,
        "tokens_total_actual": 100,
        "tokens_estimated_context": 100,
    }
    assert metrics["tokens_in_actual"] == 80
    assert metrics["tokens_out_actual"] == 20
    assert metrics["tokens_total_actual"] == 100
    assert metrics["tokens_estimated_context"] == 100


def test_legacy_mixed_counters_still_feed_the_budget_gate():
    """The budget gate (loop.py) enforces against state.tokens_in/out, which
    remain the legacy MIXED counters; the split fields must not have
    silently changed budget semantics."""
    state = fold_events(_usage_events())
    assert state.tokens_in == 180  # 100 estimate + 80 usage (legacy behaviour)
    assert state.tokens_out == 20


# --------------------------------------------------------------------------- WP-R4-01: supervised root aggregation


def _write_session_events(sessions_dir: Path, sid: str, specs: list[tuple[str, dict]]) -> str:
    import json as _json

    d = sessions_dir / sid
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, (etype, payload) in enumerate(specs, start=1):
        lines.append(_json.dumps({
            "schema_version": 1, "event_id": f"evt_{sid}_{i}", "session_id": sid,
            "seq": i, "ts": f"2026-01-01T00:00:{(i - 1):02d}Z", "type": etype, "payload": payload,
        }, ensure_ascii=False))
    (d / "events.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sid


def _root_fixture(tmp_path: Path, worker_specs: list[dict]):
    """Build a real ledger + session logs for direct root_record_fields unit
    tests. worker_specs items: {"events": [(type, payload)] | None} — None
    simulates a worker whose session event log is missing."""
    from jinlin_agent.config import load_config
    from jinlin_agent.ledger import TaskLedger
    from jinlin_agent.taskmodel import TaskSpec, WorkerRecord, new_task_id

    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    root = TaskSpec(task_id=new_task_id(), root_task_id="", title="root", verification="code")
    root = TaskSpec(task_id=root.task_id, root_task_id=root.task_id, title="root", verification="code")
    ledger.create_task(root)
    for i, spec in enumerate(worker_specs):
        sub = TaskSpec(
            task_id=new_task_id(), root_task_id=root.task_id, parent_task_id=root.task_id,
            title=f"sub {i}", verification="code",
        )
        ledger.create_task(sub)
        wid = f"worker_r4_{i:04d}"
        ledger.register_worker(WorkerRecord(worker_id=wid, task_id=sub.task_id, root_task_id=root.task_id))
        if spec.get("events") is not None:
            sid = _write_session_events(cfg.sessions_dir, f"sess_r4_{i:04d}", spec["events"])
            ledger.set_worker_session(wid, sid)
    return ledger, cfg, root.task_id


def _usage_session_events(attempts: int = 4, prompt: int = 40, completion: int = 10, omit_usage_at: int | None = None) -> list[tuple[str, dict]]:
    specs: list[tuple[str, dict]] = [("context.built", {"tokens_estimate": 25})]
    for i in range(attempts):
        if omit_usage_at is not None and i == omit_usage_at:
            specs.append(("model.attempt", {}))
        else:
            specs.append(("model.attempt", {"usage": {"prompt_tokens": prompt, "completion_tokens": completion}}))
    return specs


def test_missing_worker_session_log_marks_usage_incomplete(tmp_path):
    """A worker registered in the ledger whose session log is missing must
    fail root usage completeness and be traceable via usage_missing_sessions
    (its attempt count is unknowable, so it must NOT inflate the missing
    CALL count)."""
    from benchmark_matrix import root_record_fields

    ledger, cfg, root_id = _root_fixture(tmp_path, [
        {"events": _usage_session_events()},
        {"events": None},
    ])
    try:
        fields = root_record_fields(ledger, cfg, root_id, tmp_path, events=[])
        assert fields["usage_complete"] is False
        assert fields["usage_missing_sessions"] == 1
        assert fields["usage_expected_sessions"] == 2
        assert fields["tokens_usage_missing_calls"] == 0  # the missing log adds no fabricated calls
        # the readable worker's usage is still the observed subtotal
        assert fields["tokens_in_actual"] == 160
        assert fields["tokens_total_actual"] == fields["tokens_in_actual"] + fields["tokens_out_actual"]
    finally:
        ledger.close()


def test_supervised_root_all_missing_usage_is_null_not_zero(tmp_path):
    from benchmark_matrix import root_record_fields

    all_missing = [("context.built", {"tokens_estimate": 25})] + [("model.attempt", {}) for _ in range(4)]
    ledger, cfg, root_id = _root_fixture(tmp_path, [
        {"events": list(all_missing)},
        {"events": list(all_missing)},
    ])
    try:
        fields = root_record_fields(ledger, cfg, root_id, tmp_path, events=[])
        assert fields["tokens_in_actual"] is None
        assert fields["tokens_out_actual"] is None
        assert fields["tokens_total_actual"] is None
        assert fields["usage_complete"] is False
        assert fields["tokens_usage_missing_calls"] == 8
    finally:
        ledger.close()


def test_supervised_root_partial_usage_keeps_partial_sum_but_marks_incomplete(tmp_path):
    from benchmark_matrix import root_record_fields

    ledger, cfg, root_id = _root_fixture(tmp_path, [
        {"events": _usage_session_events()},
        {"events": _usage_session_events(attempts=4, omit_usage_at=2)},
    ])
    try:
        fields = root_record_fields(ledger, cfg, root_id, tmp_path, events=[])
        assert fields["tokens_in_actual"] == 40 * 7  # 7 observed attempts across the two workers
        assert fields["tokens_out_actual"] == 10 * 7
        assert fields["tokens_total_actual"] == fields["tokens_in_actual"] + fields["tokens_out_actual"]
        assert fields["usage_complete"] is False
        assert fields["tokens_usage_missing_calls"] == 1
    finally:
        ledger.close()


def test_supervised_mock_provider_reports_null_actual_usage(tmp_path, monkeypatch):
    """WP-R4-01 through the real supervised pipeline (planner AND multi):
    with no provider usage anywhere, the root record reports null actuals —
    never folded-to-zero — and the summary counts the runs as without
    usage."""
    from pathlib import Path as _P

    from jinlin_agent.model import mock as mock_module
    from jinlin_agent.model.mock import MockProvider as RealMockProvider

    for strategy in ("planner", "multi"):
        nth = {"n": 0}

        def factory(script=None, _nth=nth):
            _nth["n"] += 1
            if _nth["n"] == 1:
                return RealMockProvider([_ASSESS_PLAN, _DRY_PLAN])
            return RealMockProvider(_DRY_RUN_SCRIPT)

        monkeypatch.setattr(mock_module, "MockProvider", factory)
        out = tmp_path / strategy
        record = run_one(TASK, strategy, dry_run=True, out_dir=out)
        assert record["status"] == "completed", record
        assert record["tokens_in_actual"] is None
        assert record["tokens_out_actual"] is None
        assert record["tokens_total_actual"] is None
        assert record["usage_complete"] is False
        assert record["usage_missing_sessions"] == 0
        assert record["tokens_usage_missing_calls"] >= 1
        summary = aggregate([record])
        assert summary[strategy]["runs_with_usage"] == 0
        assert summary[strategy]["runs_without_usage"] == 1


def _usage_worker_script(prompt: int, completion: int, estimate_hint: int) -> list[dict]:
    return [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}], "prompt_tokens": prompt, "completion_tokens": completion},
        {"tool_calls": [{"name": "update_plan", "arguments": {"plan": {"objective": "x", "steps": [{"description": "d", "status": "completed", "evidence": ["README.md"]}]}}}], "prompt_tokens": prompt, "completion_tokens": completion},
        {"tool_calls": [{"name": "propose_completion", "arguments": {"summary": "s", "claims": [{"statement": "done", "evidence_refs": ["README.md"]}]}}], "prompt_tokens": prompt, "completion_tokens": completion},
    ]


def test_supervised_root_total_actual_equals_input_plus_output(tmp_path, monkeypatch):
    from jinlin_agent.model import mock as mock_module
    from jinlin_agent.model.mock import MockProvider as RealMockProvider

    nth = {"n": 0}

    def factory(script=None):
        nth["n"] += 1
        if nth["n"] == 1:
            return RealMockProvider([_ASSESS_PLAN, _DRY_PLAN])
        return RealMockProvider(_usage_worker_script(50, 20, 100))

    monkeypatch.setattr(mock_module, "MockProvider", factory)
    record = run_one(TASK, "planner", dry_run=True, out_dir=tmp_path)
    assert record["status"] == "completed", record
    assert record["usage_complete"] is True
    assert record["tokens_total_actual"] == record["tokens_in_actual"] + record["tokens_out_actual"]
    assert record["tokens_in_actual"] > 0 and record["tokens_out_actual"] > 0


def test_supervised_root_sums_provider_usage_without_context_estimates(tmp_path, monkeypatch):
    from jinlin_agent.model import mock as mock_module
    from jinlin_agent.model.mock import MockProvider as RealMockProvider

    nth = {"n": 0}

    def factory(script=None):
        nth["n"] += 1
        if nth["n"] == 1:
            return RealMockProvider([_ASSESS_PLAN, _DRY_PLAN])
        # two workers with DIFFERENT usage and (internally generated, hence
        # different) context estimates: only the usage may land in actual
        return RealMockProvider(_usage_worker_script(30 if nth["n"] == 2 else 70, 10, 0))

    monkeypatch.setattr(mock_module, "MockProvider", factory)
    record = run_one(TASK, "planner", dry_run=True, out_dir=tmp_path)
    assert record["verification_passed"] is not None  # pipeline completed sensibly
    subs = record["subtask_integration"]
    assert len(subs) == 2
    # each worker ran 3 attempts: 3 * (30 + 10) + 3 * (70 + 10)
    assert record["tokens_in_actual"] == 3 * 30 + 3 * 70
    assert record["tokens_out_actual"] == 3 * 10 + 3 * 10
    assert record["tokens_total_actual"] == record["tokens_in_actual"] + record["tokens_out_actual"]
    assert record["tokens_estimated_context"] > 0
    assert record["usage_complete"] is True


def test_aggregate_does_not_count_null_actual_as_run_with_usage():
    rows = [
        {"strategy": "multi", "status": "completed", "verification_passed": False,
         "wall_seconds": 1.0, "tokens_in_actual": 0, "tokens_out_actual": 0,
         "tokens_total_actual": None, "tokens_estimated_context": 8,
         "tokens_usage_missing_calls": 8, "usage_complete": False,
         "model_calls": 2, "replans": 0, "human_interventions": 0},
        {"strategy": "multi", "status": "completed", "verification_passed": True,
         "wall_seconds": 1.0, "tokens_in_actual": None, "tokens_out_actual": None,
         "tokens_total_actual": None, "tokens_estimated_context": 9,
         "tokens_usage_missing_calls": 3, "usage_complete": False,
         "model_calls": 3, "replans": 0, "human_interventions": 0},
    ]
    summary = aggregate(rows)
    # the 0/0 run DOES carry observed usage (explicit numeric zero, complete=false);
    # the null run does not count as usage at all
    assert summary["multi"]["runs_with_usage"] == 1
    assert summary["multi"]["usage_complete_runs"] == 0
    assert summary["multi"]["runs_with_partial_usage"] == 1
    assert summary["multi"]["runs_without_usage"] == 1
    assert summary["multi"]["tokens_in_actual_total"] == 0  # legitimate zero, kept
    assert summary["multi"]["tokens_total_actual_total"] is None  # null total not summed

