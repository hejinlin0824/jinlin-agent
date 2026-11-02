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

elif point == "after-clean-quarantine":
    real_replace = os.replace
    def hooked_replace(source, destination):
        result = real_replace(source, destination)
        if ".jinlin-clean-" in Path(destination).name:
            exit_now()
        return result
    os.replace = hooked_replace
    trash._clean_rescue_impl(workspace, apply=True)

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
    ("point", "expected_stage", "recovery_status"),
    [
        ("after-stage-create", b"", "incomplete"),
        ("after-partial-write", PAYLOAD[:7], "incomplete"),
        ("after-file-fsync", PAYLOAD, "restored"),
        ("after-stage-directory-sync", PAYLOAD, "restored"),
    ],
)
def test_real_child_process_crash_before_publish_is_reconciled_safely(
    tmp_path, point, expected_stage, recovery_status
):
    rescue, stage, target = _fixture(tmp_path, point)
    _crash(tmp_path, rescue, point)

    assert stage.is_file()
    assert stage.read_bytes() == expected_stage
    assert not target.exists()
    assert rescue.read_bytes() == PAYLOAD

    recovered = trash._recover_rescue_impl(tmp_path)
    assert recovered["items"]
    assert recovered["items"][0]["status"] == recovery_status
    if recovery_status == "incomplete":
        assert not target.exists()
        inspection = trash.inspect_rescue(tmp_path)
        assert inspection["status"] == "attention-required"
        assert inspection["counts"]["incomplete_stages"] == 1
    else:
        assert target.read_bytes() == PAYLOAD
        inspection = trash.inspect_rescue(tmp_path)
        assert inspection["counts"]["stages"] == 1
    assert stage.read_bytes() == expected_stage
    assert rescue.read_bytes() == PAYLOAD


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


def test_real_child_process_crash_after_cleanup_quarantine_preserves_evidence(tmp_path):
    rescue, stage, target = _fixture(tmp_path, "cleanup-crash")
    restored = trash._restore_rescue_impl(tmp_path, rescue.name)
    assert restored["status"] == "restored"
    assert target.read_bytes() == PAYLOAD

    _crash(tmp_path, rescue, "after-clean-quarantine")
    assert target.read_bytes() == PAYLOAD

    inspection = trash.inspect_rescue(tmp_path)
    assert inspection["status"] == "attention-required"
    assert inspection["counts"]["quarantined"] == 1
    quarantine_path = tmp_path / inspection["quarantined"][0]["path"]
    assert quarantine_path.is_file()
    assert quarantine_path.read_bytes() == PAYLOAD
    # The cleaner must not silently remove the interrupted quarantine.
    cleaned = trash._clean_rescue_impl(tmp_path, apply=True)
    assert cleaned["quarantined"]
    assert quarantine_path.exists()
    assert target.read_bytes() == PAYLOAD


@pytest.mark.parametrize(
    ("crash_point", "exit_code"),
    [("after-quarantine-publish", 93), ("after-quarantine-manifest", 94)],
)
def test_real_child_process_crash_during_quarantine_recovery_is_idempotent(tmp_path, crash_point, exit_code):
    rescue = tmp_path / ".quarantine-crash.py.jinlin-rescue-c0ffee02"
    rescue.write_bytes(PAYLOAD)
    assert trash.record_trash(tmp_path, rescue, "quarantine-crash.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-c0ffee02")
    os.replace(rescue, quarantine)

    worker = r'''
import os
import sys
from pathlib import Path
import jinlin_agent.trash as trash
workspace = Path(sys.argv[1])
point = sys.argv[2]
if point == "after-quarantine-publish":
    real_link = os.link
    def hooked_link(source, destination, *args, **kwargs):
        result = real_link(source, destination, *args, **kwargs)
        if ".jinlin-quarantine-restore-" in Path(source).name:
            os._exit(93)
        return result
    os.link = hooked_link
elif point == "after-quarantine-manifest":
    real_write = trash._write_manifest_atomic
    def hooked_write(*args, **kwargs):
        result = real_write(*args, **kwargs)
        os._exit(94)
        return result
    trash._write_manifest_atomic = hooked_write
trash.recover_clean_quarantines(workspace, apply=True)
raise SystemExit("crash hook was not reached")
'''
    completed = subprocess.run(
        [sys.executable, "-c", worker, str(tmp_path), crash_point],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == exit_code, (
        f"child did not stop at {crash_point}; rc={completed.returncode}\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )

    # The quarantine evidence survives both crash boundaries. A fresh process
    # can reconcile the published candidate and finish metadata/stage cleanup.
    assert rescue.read_bytes() == PAYLOAD
    assert quarantine.read_bytes() == PAYLOAD
    recovered = trash.recover_clean_quarantines(tmp_path, apply=True)
    assert recovered["status"] == "applied"
    assert recovered["recovered"][0]["status"] == "already-restored"
    assert trash.list_rescue(tmp_path)["rescued"][0]["path"] == rescue.name
    assert quarantine.read_bytes() == PAYLOAD
    assert os.stat(rescue).st_ino != os.stat(quarantine).st_ino


@pytest.mark.parametrize(("crash_point", "exit_code"), [("partial", 95), ("after-fsync", 96)])
def test_real_child_process_crash_during_audit_append_has_explicit_recovery_semantics(tmp_path, crash_point, exit_code):
    worker = r'''
import os
import sys
from pathlib import Path
import jinlin_agent.trash as trash
workspace = Path(sys.argv[1])
point = sys.argv[2]
audit_fd = None
real_open = os.open
real_write = os.write
real_fsync = os.fsync
def hooked_open(path, flags, *args, **kwargs):
    global audit_fd
    fd = real_open(path, flags, *args, **kwargs)
    if Path(path).name == "rescue-audit.jsonl":
        audit_fd = fd
    return fd
def hooked_write(fd, data):
    if fd == audit_fd and point == "partial":
        real_write(fd, data[:max(1, len(data) // 2)])
        os._exit(95)
    return real_write(fd, data)
def hooked_fsync(fd):
    real_fsync(fd)
    if fd == audit_fd and point == "after-fsync":
        os._exit(96)
os.open = hooked_open
os.write = hooked_write
os.fsync = hooked_fsync
trash._append_audit_event(
    workspace, "restore", "restored", txid="a" * 32,
    rescue=".audit-crash.py.jinlin-rescue-c0ffee03",
    target="audit-crash.py", sha256="b" * 64,
)
raise SystemExit("crash hook was not reached")
'''
    completed = subprocess.run(
        [sys.executable, "-c", worker, str(tmp_path), crash_point],
        cwd=Path(__file__).resolve().parents[1],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == exit_code, (
        f"audit child did not stop at {crash_point}; rc={completed.returncode}\n"
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )

    audit = trash._read_audit(tmp_path)
    if crash_point == "partial":
        assert audit["events"] == []
        assert audit["issues"]
        ok, warning = trash._append_audit_event(tmp_path, "recover", "already-published", txid="a" * 32)
        assert not ok and "integrity issues" in warning
    else:
        assert audit["issues"] == []
        assert len(audit["events"]) == 1
        assert audit["events"][0]["seq"] == 1
        assert audit["events"][0]["txid"] == "a" * 32
        ok, warning = trash._append_audit_event(tmp_path, "recover", "already-published", txid="a" * 32)
        assert ok and warning is None
        retried = trash._read_audit(tmp_path)
        assert retried["issues"] == []
        assert [event["seq"] for event in retried["events"]] == [1, 2]
