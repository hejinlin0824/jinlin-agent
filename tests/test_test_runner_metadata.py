"""Release metadata and one-click runner contract tests."""
from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_test_suite_metadata_declares_current_package_and_validation_matrix():
    metadata = json.loads((ROOT / "test-suite.json").read_text(encoding="utf-8"))

    assert metadata["schema_version"] == 1
    assert metadata["runner_contract_version"] == 3
    assert metadata["suite_version"] == "2026.11.03"
    assert metadata["task_package"] == {
        "id": "WP-2",
        "name": "本地工作台与可靠任务执行",
        "version": "2.1.0",
    }
    assert "Full pytest suite with JUnit XML report" in metadata["checks"]
    assert "Harness conformance evaluation (all offline probes)" in metadata["checks"]
    assert "Rescue fault-injection, crash and seeded-chaos matrix" in metadata["checks"]
    assert len(metadata["manual_acceptance"]) >= 3


def test_powershell_runner_enforces_contract_and_supports_full_and_fast_modes():
    script = (ROOT / "scripts" / "test-all.ps1").read_text(encoding="utf-8")

    match = re.search(r"\$RunnerContractVersion\s*=\s*(\d+)", script)
    assert match, "PowerShell runner must declare its expected metadata contract"
    metadata = json.loads((ROOT / "test-suite.json").read_text(encoding="utf-8"))
    assert int(match.group(1)) == metadata["runner_contract_version"]

    for required in (
        "STALE TEST RUNNER",
        "Task package :",
        "Branch / SHA :",
        "ALL CHECKS PASSED",
        "junit-",
        "report-",
        "Start-Transcript",
        "test_read_only_workflow.py",
        "test_workbench_tasks.py",
        "rescue_smoke.py",
        "test-rescue-fault-matrix.py",
        "Fast",
        "SkipInstall",
    ):
        assert required in script


def test_windows_double_click_entrypoint_preserves_exit_code_and_pauses():
    script = (ROOT / "test-all.bat").read_text(encoding="utf-8")

    assert "scripts\\test-all.ps1" in script
    assert "ExecutionPolicy Bypass" in script
    assert "RESULT=%ERRORLEVEL%" in script
    assert "pause" in script
    assert "exit /b %RESULT%" in script
