"""Release metadata and test-runner identity contract checks."""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_test_suite_metadata_declares_current_task_package_and_checks():
    metadata = json.loads((ROOT / "test-suite.json").read_text(encoding="utf-8"))

    assert metadata["schema_version"] == 1
    assert metadata["runner_contract_version"] == 2
    assert metadata["suite_version"]
    assert metadata["task_package"] == {
        "id": "06",
        "name": "工程化、可观测性与正式交付",
        "version": "1.0.0",
    }
    assert metadata["checks"] == [
        "Full pytest suite",
        "Harness evaluation (all)",
        "Rescue lifecycle end-to-end smoke test",
        "Rescue fault-injection + crash + seeded-chaos matrix",
        "Rescue CLI command registration",
    ]


def test_powershell_runner_enforces_metadata_contract_and_reports_identity():
    script = (ROOT / "scripts" / "test-all.ps1").read_text(encoding="utf-8")

    match = re.search(r"\$RunnerContractVersion\s*=\s*(\d+)", script)
    assert match, "PowerShell runner must declare its expected metadata contract"
    metadata = json.loads((ROOT / "test-suite.json").read_text(encoding="utf-8"))
    assert int(match.group(1)) == metadata["runner_contract_version"]

    for required in (
        "STALE TEST RUNNER",
        "Task package :",
        "Package ver. :",
        "Suite version:",
        "Branch / SHA :",
        "ALL CHECKS PASSED",
    ):
        assert required in script
