"""WP-R2 regression: integration verification runs through the Sandbox
abstraction — never through a bare subprocess fallback (review task book §三,
WP-R2).

Trust boundary: git plumbing in the coordinator stays argv-based (management
operations, not untrusted code); the project's own TEST commands execute
untrusted repository code and therefore MUST go through Sandbox.run, with a
fail-closed posture when the sandbox cannot be built or refuses.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from jinlin_agent.config import load_config
from jinlin_agent.errors import SANDBOX_UNAVAILABLE
from jinlin_agent.ledger import TaskLedger
from jinlin_agent.sandbox import NullSandbox
from jinlin_agent.sandbox.base import RunResult
from jinlin_agent.workspace_manager import IntegrationCoordinator, WorkspaceManager

from test_worktree import make_git_repo


class RecordingSandbox:
    """Deterministic stand-in that records how it was invoked."""

    level = "L2-fake"
    engine = "fake"

    def __init__(self, path: str, exit_code: int = 0, timed_out: bool = False):
        self.workspace = path
        self.calls: list[dict] = []
        self._exit_code = exit_code
        self._timed_out = timed_out

    def run(self, argv, cwd, timeout, env=None, stdin_data=None, max_output_chars=200_000) -> RunResult:
        self.calls.append({"argv": argv, "cwd": cwd, "timeout": timeout})
        return RunResult(
            exit_code=None if self._timed_out else self._exit_code,
            stdout="1 passed" if not self._timed_out else "",
            stderr="",
            timed_out=self._timed_out,
            duration_ms=5,
            engine=self.engine,
        )


class RefusingSandbox(RecordingSandbox):
    def run(self, argv, cwd, timeout, env=None, stdin_data=None, max_output_chars=200_000) -> RunResult:
        raise pytest.fail.Exception("subprocess fallback was used — the sandbox must be the only executor")


class ExplodingSandbox:
    level = "L0"
    engine = "none"

    def __init__(self, path):
        raise SANDBOX_UNAVAILABLE_Error()


class SANDBOX_UNAVAILABLE_Error(Exception):
    pass


def make_coordinator(tmp_path, sandbox_factory, repo=None):
    repo = repo or make_git_repo(tmp_path / "repo")
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    manager = WorkspaceManager(repo, cfg, ledger)
    coordinator = IntegrationCoordinator(repo, cfg, ledger, root_id="root_r2", manager=manager, sandbox_factory=sandbox_factory)
    ws = manager.create_worker_workspace("task_r2")
    return coordinator, manager, ledger, ws, cfg


from contextlib import contextmanager


@contextmanager
def no_subprocess_fallback():
    """Context guard: any DIRECT subprocess use while armed fails the test.
    Arm it only around the integrate call — test fixtures legitimately use
    subprocess for git setup."""

    original_run = subprocess.run

    def _boom(*args, **kwargs):
        argv = list(args[0]) if args else list(kwargs.get("args") or [])
        first = str(argv[0]).lower() if argv else ""
        if first == "git":
            # git plumbing is a separate, management-only trust boundary (WP-R2 note)
            kwargs.pop("args", None)
            return original_run(argv, **kwargs)
        raise AssertionError(f"bare subprocess was used instead of the sandbox: {argv}")

    with pytest.MonkeyPatch.context() as mp:
        # Note: subprocess.Popen is intentionally NOT patched — the git
        # plumbing delegate needs the real run()'s internals. The guard's
        # assertion is exactly the historical defect shape: the project test
        # command reaching subprocess.run directly.
        mp.setattr(subprocess, "run", _boom)
        yield


# --------------------------------------------------------------------------- enforcement


def test_verification_executes_through_the_sandbox(tmp_path):
    coordinator, manager, ledger, ws, cfg = make_coordinator(tmp_path, lambda path: RecordingSandbox(str(path)))
    try:
        outcome = coordinator.integrate("task_r2", ws)
        assert outcome["status"] == "integrated", outcome
        fake = coordinator._sandbox_factory.__self__ if hasattr(coordinator._sandbox_factory, "__self__") else None
        # evidence records which sandbox ran the tests
        assert outcome["verification"]["sandbox"]["level"] == "L2-fake"
        assert outcome["verification"]["sandbox"]["engine"] == "fake"
    finally:
        manager.cleanup_all()
        ledger.close()


def test_sandbox_refusal_is_fail_closed_without_subprocess_fallback(tmp_path):
    calls = []

    def refusing_factory(path):
        sandbox = RefusingSandbox(str(path))
        orig = sandbox.run

        def run(argv, cwd, timeout, env=None, stdin_data=None, max_output_chars=200_000):
            calls.append(argv)
            raise SANDBOX_UNAVAILABLE_Error("sandbox refused execution")

        sandbox.run = run
        return sandbox

    coordinator, manager, ledger, ws, cfg = make_coordinator(tmp_path, refusing_factory)
    try:
        with no_subprocess_fallback():
            outcome = coordinator.integrate("task_r2", ws)
        assert outcome["status"] == "verification_failed"
        assert "sandbox" in outcome["verification"]["note"].lower()
        assert outcome["verification"]["exit_code"] is None
    finally:
        manager.cleanup_all()
        ledger.close()


def test_sandbox_construction_failure_is_fail_closed(tmp_path):
    def broken_factory(path):
        raise RuntimeError("restricted token construction failed")

    coordinator, manager, ledger, ws, cfg = make_coordinator(tmp_path, broken_factory)
    try:
        with no_subprocess_fallback():
            outcome = coordinator.integrate("task_r2", ws)
        assert outcome["status"] == "verification_failed"
        assert "sandbox" in outcome["verification"]["note"].lower()
    finally:
        manager.cleanup_all()
        ledger.close()


def test_sandbox_timeout_rolls_back_the_merge(tmp_path):
    coordinator, manager, ledger, ws, cfg = make_coordinator(tmp_path, lambda path: RecordingSandbox(str(path), timed_out=True))
    try:
        (ws.path / "calc.py").write_text("def add(a, b):\n    return a + b  # x\n", encoding="utf-8")
        outcome = coordinator.integrate("task_r2", ws)
        assert outcome["status"] == "verification_failed"
        assert outcome["verification"]["exit_code"] is None
        # the merge is rolled back: the change is not on the integration tree
        current = (coordinator.integration_path() / "calc.py").read_text(encoding="utf-8")
        assert "# x" not in current
    finally:
        manager.cleanup_all()
        ledger.close()


def test_verify_runs_in_the_integration_tree(tmp_path):
    recorded = {}

    class Capture(RecordingSandbox):
        def run(self, argv, cwd, timeout, env=None, stdin_data=None, max_output_chars=200_000):
            recorded["cwd"] = cwd
            recorded["argv"] = argv
            return super().run(argv, cwd, timeout, env, stdin_data, max_output_chars)

    coordinator, manager, ledger, ws, cfg = make_coordinator(tmp_path, lambda path: Capture(str(path)))
    try:
        with no_subprocess_fallback():
            outcome = coordinator.integrate("task_r2", ws)
        assert outcome["status"] == "integrated"
        assert Path(recorded["cwd"]).resolve() == coordinator.integration_path().resolve()
    finally:
        manager.cleanup_all()
        ledger.close()


# --------------------------------------------------------------------------- Windows boundary (L1)


@pytest.mark.skipif(sys.platform != "win32", reason="L1 restricted-token sandbox is Windows-only")
def test_malicious_integration_test_cannot_escape_the_worktree(tmp_path):
    """A test inside the integration tree that writes OUTSIDE the integration
    workspace is denied by the L1 sandbox; verification fails and the merge
    rolls back."""
    coordinator, manager, ledger, ws, cfg = make_coordinator(tmp_path, None, repo=make_git_repo(tmp_path / "repo"))
    try:
        evil = coordinator.integration_path() / "tests" / "test_escape.py"
        evil.parent.mkdir(parents=True, exist_ok=True)
        escape_target = tmp_path / "outside.txt"
        evil.write_text(
            "def test_escape():\n"
            f"    with open(r'{escape_target}', 'w') as fh:\n"
            "        fh.write('escaped')\n",
            encoding="utf-8",
        )
        outcome = coordinator.integrate("task_r2", ws)
        assert outcome["status"] == "verification_failed"
        assert not escape_target.exists(), "the sandboxed verification must not write outside the worktree"
    finally:
        manager.cleanup_all()
        ledger.close()
