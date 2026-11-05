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

from benchmark_matrix import _session_metrics, aggregate, run_one  # noqa: E402

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
