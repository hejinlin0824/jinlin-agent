"""The harness claim, executable: T1-T4 conformance probes and E1-E8 evaluations.

Each probe builds a tiny fixture repo, drives the harness with a scripted model,
and asserts over the event log and the real workspace. If this file is green, the
"it is a harness, not a demo" claim is backed by tests.
"""

from __future__ import annotations

import pytest

from jinlin_agent.evalkit import E_PROBES, HCS_PROBES, V2_PROBES, run_probe


@pytest.mark.parametrize("probe", HCS_PROBES, ids=lambda p: f"hcs-{p.name}")
def test_hcs_conformance(probe):
    ok, detail = run_probe(probe)
    assert ok, f"{probe.name} {probe.title}: {detail}"


@pytest.mark.parametrize("probe", E_PROBES, ids=lambda p: f"eval-{p.name}")
def test_task_evaluations(probe):
    ok, detail = run_probe(probe)
    assert ok, f"{probe.name} {probe.title}: {detail}"


@pytest.mark.parametrize("probe", V2_PROBES, ids=lambda p: f"v2-{p.name}")
def test_v2_task_layer(probe):
    ok, detail = run_probe(probe)
    assert ok, f"{probe.name} {probe.title}: {detail}"
