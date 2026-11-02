"""Subprocess crash/restart tests for Rescue restore transaction boundaries.

The child process calls os._exit() after a real filesystem operation. This
models abrupt process termination (not kernel crash or power loss).
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

import jinlin_agent.trash as trash


PAYLOAD = b"subprocess-crash-original-bytes\n"
WORKER = r'''
import os
import sys
from pathlib import Path
import jinlin_agent.trash as trash

workspace = Path(sys.argv[1])
rescue_rel = sys.argv[2]
point = sys.argv[3]
exit_now = lambda: os._exit(91)

if point == "after-stage-create":
    real_open = os.open
    def hooked_open(path, flags, *args, **kwargs):
        fd = real_open(path, flags, *args, **kwargs)
        if ".jinlin-restore-" in Path(path).name and flags & os.O_CREAT:
            exit_now()
        return fd
    os.open = hooked_open

elif point == "after-partial-write":
    real_write = os.write
    def hooked_write(fd, data):
        count = real_write(fd, data[:max(1, min(7, len(data)))])
        exit_now()
        return count
    os.write = hooked_write

elif point == "after-file-fsync":
    real_fsync = os.fsync
    def hooked_fsync(fd):
        real_fsync(fd)
        exit_now()
    os.fsync = hooked_fsync

elif point == "after-stage-directory-sync":
    real_sync = trash._fsync_directory
    def hooked_sync(directory):
        result = real_sync(directory)
        exit_now()
        return result
    trash._fsync_directory = hooked_sync

elif point == "after-publish":
    real_link = os.link
    def hooked_link(source, destination, *args, **kwargs):
        result = real_link(source, destination, *args, **kwargs)
        if ".jinlin-restore-" in Path(source).name:
            exit_now()
        return result
    os.link = hooked_link

else:
    raise SystemExit("unknown crash point")

trash._restore_rescue_impl(workspace, rescue_rel)
raise SystemExit("crash hook was not reached")
'''


def _fixture(workspace: Path, stem: str):
    rescue = workspace / f".{stem}.py.jinlin-rescue-c0ffee01"
    rescue.write_bytes(PAYLOAD)
    assert trash.record_trash(workspace, rescue, f"{stem}.py", "rescue")
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    stage = trash._restore_stage_path(workspace / f"{stem}.py", digest)
    target = workspace / f"{stem}.py"
    return rescue, stage, target


def _crash(workspace: Path, rescue: Path, point: str) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        [sys.executable, "-c", WORKER, str(workspace), rescue.name, point],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 91, (
        f"child did not terminate at {point}; rc={completed.returncode}\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    return completed


@pytest.mark.parametrize(
    ("point", "expected_stage"),
    [
        ("after-stage-create", b""),
        ("after-partial-write", PAYLOAD[:7]),
        ("after-file-fsync", PAYLOAD),
        ("after-stage-directory-sync", PAYLOAD),
    ],
)
def test_real_child_process_crash_before_publish_is_reconciled_safely(
    tmp_path, point, expected_stage
):
    rescue, stage, target = _fixture(tmp_path, point)
    _crash(tmp_path, rescue, point)

    assert stage.is_file()
    assert stage.read_bytes() == expected_stage
    assert not target.exists()
    assert rescue.read_bytes() == PAYLOAD

    recovered = trash._recover_rescue_impl(tmp_path)
    assert recovered["items"]
    assert recovered["items"][0]["status"] == "incomplete"
    assert not target.exists()
    assert stage.read_bytes() == expected_stage
    assert rescue.read_bytes() == PAYLOAD

    inspection = trash.inspect_rescue(tmp_path)
    assert inspection["status"] == "attention-required"
    assert inspection["counts"]["stages"] == 1


def test_real_child_process_crash_immediately_after_publish_is_idempotent(tmp_path):
    rescue, stage, target = _fixture(tmp_path, "after-publish")
    _crash(tmp_path, rescue, "after-publish")

    # The hard link happened, but the child died before post-publication checks.
    assert target.read_bytes() == PAYLOAD
    assert stage.read_bytes() == PAYLOAD
    assert rescue.read_bytes() == PAYLOAD

    recovered = trash._recover_rescue_impl(tmp_path)
    assert recovered["items"][0]["status"] == "already-published"
    assert target.read_bytes() == PAYLOAD
    assert stage.read_bytes() == PAYLOAD
    assert rescue.read_bytes() == PAYLOAD

    # Re-running recovery is safe and does not duplicate or overwrite content.
    again = trash._recover_rescue_impl(tmp_path)
    assert again["items"][0]["status"] == "already-published"
    assert target.read_bytes() == PAYLOAD
