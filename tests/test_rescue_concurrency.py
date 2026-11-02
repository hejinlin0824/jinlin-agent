"""Cross-process contention tests for the workspace lock boundary."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jinlin_agent.locks import SessionLock, SessionLockedError
from jinlin_agent.trash import (
    clean_rescue,
    clean_trash,
    inspect_rescue,
    list_rescue,
    recover_clean_quarantines,
    recover_rescue,
    restore_rescue,
)


_HOLDER = r"""
import sys
import time
from pathlib import Path
from jinlin_agent.locks import SessionLock

lock_path = Path(sys.argv[1])
ready = Path(sys.argv[2])
release = Path(sys.argv[3])
lock = SessionLock(lock_path)
lock.acquire()
ready.write_text("ready", encoding="utf-8")
deadline = time.monotonic() + 15
while not release.exists() and time.monotonic() < deadline:
    time.sleep(0.01)
lock.release()
"""


def _start_holder(workspace: Path):
    lock_path = workspace / ".jinlin" / "write.lock"
    ready = workspace / "holder.ready"
    release = workspace / "holder.release"
    process = subprocess.Popen(
        [sys.executable, "-c", _HOLDER, str(lock_path), str(ready), str(release)],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=2)
            pytest.fail(f"lock holder exited early: {process.returncode}\n{stdout}\n{stderr}")
        time.sleep(0.01)
    if not ready.exists():
        process.kill()
        stdout, stderr = process.communicate(timeout=2)
        pytest.fail(f"lock holder did not acquire lock\n{stdout}\n{stderr}")
    return process, release


@pytest.mark.parametrize(
    "operation",
    [
        lambda ws: clean_trash(ws, apply=True),
        lambda ws: list_rescue(ws),
        lambda ws: restore_rescue(ws, ".missing.py.jinlin-rescue-deadbeef"),
        lambda ws: recover_rescue(ws),
        lambda ws: clean_rescue(ws, apply=True),
        lambda ws: recover_clean_quarantines(ws, apply=True),
        lambda ws: inspect_rescue(ws),
    ],
    ids=[
        "trash-clean", "rescue-list", "rescue-restore", "rescue-recover",
        "rescue-clean", "quarantine-recover", "rescue-inspect",
    ],
)
def test_rescue_operations_refuse_while_another_process_holds_workspace_lock(tmp_path, operation):
    process, release = _start_holder(tmp_path)
    try:
        result = operation(tmp_path)
        assert result.get("status") == "locked", result
    finally:
        release.write_text("release", encoding="utf-8")
        process.communicate(timeout=10)
    assert process.returncode == 0


def test_workspace_lock_is_released_by_abrupt_process_exit(tmp_path):
    lock_path = tmp_path / ".jinlin" / "write.lock"
    ready = tmp_path / "crashed-holder.ready"
    worker = r"""
import os
import sys
from pathlib import Path
from jinlin_agent.locks import SessionLock
lock = SessionLock(Path(sys.argv[1]))
lock.acquire()
Path(sys.argv[2]).write_text("locked", encoding="utf-8")
os._exit(73)
"""
    process = subprocess.run(
        [sys.executable, "-c", worker, str(lock_path), str(ready)],
        cwd=Path(__file__).resolve().parents[1],
        timeout=10,
        check=False,
    )
    assert process.returncode == 73
    assert ready.exists()

    contender = SessionLock(lock_path)
    contender.acquire()
    contender.release()


def test_lock_file_is_persistent_across_release(tmp_path):
    lock_path = tmp_path / ".jinlin" / "write.lock"
    first = SessionLock(lock_path)
    first.acquire()
    first.release()
    before = os.stat(lock_path, follow_symlinks=False)

    second = SessionLock(lock_path)
    second.acquire()
    second.release()
    after = os.stat(lock_path, follow_symlinks=False)

    assert (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)
    assert not lock_path.is_symlink()


@pytest.mark.skipif(os.name == "nt" or not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is POSIX-specific")
def test_lock_file_symlink_is_rejected_without_touching_target(tmp_path):
    target = tmp_path / "outside.lock"
    target.write_bytes(b"outside")
    lock_path = tmp_path / ".jinlin" / "write.lock"
    lock_path.parent.mkdir(parents=True)
    lock_path.symlink_to(target)

    with pytest.raises(SessionLockedError, match="cannot safely open session lock"):
        SessionLock(lock_path).acquire()
    assert target.read_bytes() == b"outside"
