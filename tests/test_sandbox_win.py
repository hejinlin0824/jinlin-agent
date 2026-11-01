"""Tests for the Windows L1 sandbox (restricted token + Job Object).

Run with::

    python -m pytest tests/test_sandbox_win.py -q

The tests exercise the *real* engine on the current machine: a Job Object for
process-tree containment and, when available, a WRITE_RESTRICTED token for
filesystem write confinement.  If the restricted-token path is unavailable the
tests assert the honest ``L1-job``/``job-object`` downgrade instead of skipping
the whole suite -- the Job Object path must always be truly validated.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:  # allow running without `pip install -e .`
    sys.path.insert(0, str(SRC))

from jinlin_agent.sandbox.base import RunResult, Sandbox  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only sandbox")

# The sandbox module is Windows-only by design (it binds kernel32/advapi32 at
# import time), so it must not be imported during collection on other platforms.
if os.name == "nt":
    from jinlin_agent.sandbox.windows import WindowsSandbox  # noqa: E402
    import jinlin_agent.sandbox.windows as windows_module  # noqa: E402
else:  # pragma: no cover - collection happens on non-Windows CI
    WindowsSandbox = None  # type: ignore[assignment]
    windows_module = None  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _pid_is_running(pid: int) -> bool:
    """Ask tasklist whether a PID still exists (localised output tolerated)."""
    completed = subprocess.run(
        ["tasklist", "/FI", f"PID eq {int(pid)}"],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
    )
    text = (completed.stdout or "") + (completed.stderr or "")
    return str(int(pid)) in text


def _wait_pid_gone(pid: int, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_is_running(pid):
            return True
        time.sleep(0.1)
    return False


def _acl_text(path: str | os.PathLike[str]) -> str:
    completed = subprocess.run(
        ["icacls", str(path)],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=120,
        check=False,
    )
    return completed.stdout or ""


def _first_pid(text: str) -> int:
    with_pids = [token for token in text.split() if token.isdigit()]
    assert with_pids, f"child did not print a PID: {text!r}"
    return int(with_pids[0])


@pytest.fixture()
def sandbox(tmp_path: Path) -> WindowsSandbox:
    instance = WindowsSandbox(str(tmp_path), None)
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture()
def workspace(tmp_path: Path) -> str:
    return str(tmp_path)


# --------------------------------------------------------------------------- #
# 1. basic round trip
# --------------------------------------------------------------------------- #


def test_echo_roundtrip(sandbox: WindowsSandbox, workspace: str) -> None:
    result = sandbox.run(
        [sys.executable, "-c", "print('hi')"], workspace, timeout=60
    )
    assert isinstance(result, RunResult)
    assert result.error is None, result.error
    assert result.exit_code == 0
    assert "hi" in result.stdout
    assert result.timed_out is False
    assert result.engine == sandbox.engine
    assert result.pid and result.pid > 0
    assert result.duration_ms >= 0


def test_utf8_output_and_environment(sandbox: WindowsSandbox, workspace: str) -> None:
    script = (
        "import os, sys\n"
        "print('你好')\n"
        "print('env=' + os.environ.get('JINLIN_TEST_ENV', ''))\n"
        "print('utf8=' + str(sys.flags.utf8_mode))\n"
    )
    result = sandbox.run(
        [sys.executable, "-c", script],
        workspace,
        timeout=60,
        env={"JINLIN_TEST_ENV": "hello-env"},
    )
    assert result.error is None, result.error
    assert result.exit_code == 0
    assert "你好" in result.stdout
    assert "env=hello-env" in result.stdout
    assert "utf8=1" in result.stdout


def test_stdin_stderr_and_exit_code(sandbox: WindowsSandbox, workspace: str) -> None:
    reader = (
        "import sys\n"
        "data = sys.stdin.read()\n"
        "print('got:' + data)\n"
        "sys.stderr.write('errline\\n')\n"
        "raise SystemExit(3)\n"
    )
    result = sandbox.run(
        [sys.executable, "-c", reader],
        workspace,
        timeout=60,
        stdin_data="payload\n",
    )
    assert result.error is None, result.error
    assert result.exit_code == 3
    assert "got:payload" in result.stdout
    assert "errline" in result.stderr


# --------------------------------------------------------------------------- #
# 2. timeout kills the whole process tree (grandchild included)
# --------------------------------------------------------------------------- #


def test_timeout_kills_process_tree(sandbox: WindowsSandbox, workspace: str) -> None:
    script = (
        "import subprocess, sys, time\n"
        "grandchild = subprocess.Popen(\n"
        "    [sys.executable, '-c', 'import time; time.sleep(60)']\n"
        ")\n"
        "print(grandchild.pid, flush=True)\n"
        "time.sleep(60)\n"
    )
    result = sandbox.run([sys.executable, "-c", script], workspace, timeout=2)
    assert result.error is None, result.error
    assert result.timed_out is True
    grandchild_pid = _first_pid(result.stdout)
    assert result.pid is not None
    assert _wait_pid_gone(grandchild_pid), (
        f"grandchild {grandchild_pid} survived the job-object kill"
    )
    assert _wait_pid_gone(result.pid), (
        f"root process {result.pid} survived the job-object kill"
    )


# --------------------------------------------------------------------------- #
# 3. background popen + terminate_tree
# --------------------------------------------------------------------------- #


def test_popen_background(sandbox: WindowsSandbox, workspace: str) -> None:
    handle = sandbox.popen(
        [
            sys.executable,
            "-u",
            "-c",
            "import time\nprint('background-ready', flush=True)\ntime.sleep(60)\n",
        ],
        workspace,
    )
    try:
        text = ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and "background-ready" not in text:
            out, _err = handle.read_available()
            text += out
            time.sleep(0.05)
        assert "background-ready" in text, f"no background output, got {text!r}"
        assert handle.pid
        pid = handle.pid
        assert handle.poll() is None  # still running

        handle.terminate_tree()
        assert handle.wait(10) is not None
        assert _wait_pid_gone(pid), f"background process {pid} survived terminate_tree"
    finally:
        handle.close()
        handle.close()  # idempotent


def test_popen_after_normal_exit(sandbox: WindowsSandbox, workspace: str) -> None:
    handle = sandbox.popen(
        [sys.executable, "-c", "print('done')"], workspace
    )
    try:
        deadline = time.monotonic() + 15
        text = ""
        while time.monotonic() < deadline:
            out, _err = handle.read_available()
            text += out
            if handle.poll() is not None and "done" in text:
                break
            time.sleep(0.05)
        assert "done" in text
        assert handle.wait(10) == 0
    finally:
        handle.close()


# --------------------------------------------------------------------------- #
# 4. probe_write honours the real containment
# --------------------------------------------------------------------------- #


def test_probe_write_inside_workspace(sandbox: WindowsSandbox, workspace: str) -> None:
    allowed, reason = sandbox.probe_write(workspace)
    assert allowed is True, reason
    leftovers = [name for name in os.listdir(workspace) if ".jinlin_probe_" in name]
    assert not leftovers, f"probe left files behind: {leftovers}"


def test_probe_write_outside_workspace(sandbox: WindowsSandbox) -> None:
    outside = Path("C:/") / f"jinlin_probe_{uuid.uuid4().hex}.txt"
    allowed, reason = sandbox.probe_write(str(outside))
    assert not outside.exists(), "outside probe file was created"
    if sandbox.engine == "restricted-token+job-object":
        assert allowed is False, (
            "restricted-token engine allowed a write outside the workspace: " + reason
        )
    else:
        pytest.skip(
            "token engine unavailable (job-object only); outside-write probe "
            f"not applicable - {reason}"
        )


# --------------------------------------------------------------------------- #
# 5. honest reporting + close idempotency
# --------------------------------------------------------------------------- #


def test_engine_details_are_honest(sandbox: WindowsSandbox) -> None:
    details = sandbox.details
    for key in ("has_job", "restricted_token", "integrity", "downgrade_reason"):
        assert key in details, f"details is missing {key!r}"
    assert details["has_job"] is True
    assert details["job_kill_on_close"] is True
    assert isinstance(details["integrity"], str) and details["integrity"]
    assert isinstance(details["restricted_token"], bool)
    assert sandbox.level in ("L1", "L1-job")
    assert isinstance(sandbox, Sandbox)

    if sandbox.engine == "restricted-token+job-object":
        assert sandbox.level == "L1"
        assert details["restricted_token"] is True
        assert details["downgrade_reason"] is None
        assert details["workspace_acl_granted"] is True
    else:
        assert sandbox.engine == "job-object"
        assert sandbox.level == "L1-job"
        assert details["restricted_token"] is False
        assert details["downgrade_reason"], "downgrade must explain itself"

    described = sandbox.describe()
    assert described["level"] == sandbox.level
    assert described["engine"] == sandbox.engine
    assert described["workspace"] == sandbox.workspace


def test_close_is_idempotent_and_run_reports_error_after_close(
    sandbox: WindowsSandbox, workspace: str
) -> None:
    sandbox.close()
    sandbox.close()  # second close must not raise
    result = sandbox.run([sys.executable, "-c", "print('after close')"], workspace, 10)
    assert result.error is not None
    assert "closed" in result.error.lower()


def test_close_restores_workspace_acl(tmp_path: Path) -> None:
    original = _acl_text(tmp_path)
    instance = WindowsSandbox(str(tmp_path), None)
    try:
        if instance.engine != "restricted-token+job-object":
            pytest.skip("token engine unavailable; workspace ACL was never modified")
        with_ace = _acl_text(tmp_path)
        assert with_ace != original, "sandbox did not add a workspace write ACE"
    finally:
        instance.close()
    # close() removed the ACE again: the ACL is back to its original shape.
    assert _acl_text(tmp_path) == original


def test_grandchild_pipes_are_not_leaked(sandbox: WindowsSandbox, workspace: str) -> None:
    """The pipe readers must finish even when a child spawns descendants."""
    script = (
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print('parent-done', flush=True)\n"
    )
    result = sandbox.run([sys.executable, "-c", script], workspace, timeout=30)
    assert result.error is None, result.error
    assert result.exit_code == 0
    assert "parent-done" in result.stdout


# --------------------------------------------------------------------------- #
# 6. panic paths never leak exceptions
# --------------------------------------------------------------------------- #


def test_missing_command_returns_error(sandbox: WindowsSandbox, workspace: str) -> None:
    missing = f"jinlin-no-such-command-{uuid.uuid4().hex}"
    result = sandbox.run([missing], workspace, timeout=15)
    assert isinstance(result, RunResult)
    assert result.error is not None or result.exit_code not in (0, None), (
        f"missing command looked successful: {result.to_dict()}"
    )


def test_empty_argv_returns_error(sandbox: WindowsSandbox, workspace: str) -> None:
    result = sandbox.run([], workspace, timeout=5)
    assert result.error is not None
    assert result.exit_code is None


# --------------------------------------------------------------------------- #
# 7. forced downgrade must be reported honestly and must still work
# --------------------------------------------------------------------------- #


def test_downgrade_reports_job_object_honestly(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def broken_token(*_args: object, **_kwargs: object) -> object:
        raise OSError(0, "synthetic restricted-token failure")

    monkeypatch.setattr(windows_module, "_create_restricted_token", broken_token)
    instance = WindowsSandbox(str(tmp_path), None)
    try:
        assert instance.level == "L1-job"
        assert instance.engine == "job-object"
        assert instance.details["has_job"] is True
        assert instance.details["restricted_token"] is False
        assert instance.details["downgrade_reason"]
        assert "synthetic" in instance.details["downgrade_reason"]
        assert instance.details.get("workspace_acl_granted") is None  # cleaned up

        result = instance.run(
            [sys.executable, "-c", "print('job-object works')"], str(tmp_path), 60
        )
        assert result.error is None, result.error
        assert result.exit_code == 0
        assert "job-object works" in result.stdout

        # Job-object containment must be real: kill a grandchild on timeout.
        script = (
            "import subprocess, sys, time\n"
            "p = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(60)'])\n"
            "print(p.pid, flush=True)\n"
            "time.sleep(60)\n"
        )
        timed = instance.run([sys.executable, "-c", script], str(tmp_path), 2)
        assert timed.error is None, timed.error
        assert timed.timed_out is True
        grandchild_pid = _first_pid(timed.stdout)
        assert _wait_pid_gone(grandchild_pid), (
            f"job-object fallback leaked grandchild {grandchild_pid}"
        )
    finally:
        instance.close()
