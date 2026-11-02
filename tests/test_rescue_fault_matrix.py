"""Deterministic fault-injection matrix for Rescue transaction boundaries.

These tests simulate failures at syscall boundaries. They are not a claim of
real power-loss testing; abrupt process termination and filesystem durability
remain separate validation layers.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

import jinlin_agent.trash as trash


PAYLOAD = b"fault-matrix-original-payload\\n"


def _fixture(workspace: Path, stem: str):
    rescue = workspace / f".{stem}.py.jinlin-rescue-a0b0c0d0"
    rescue.write_bytes(PAYLOAD)
    assert trash.record_trash(workspace, rescue, f"{stem}.py", "rescue")
    digest = hashlib.sha256(PAYLOAD).hexdigest()
    stage = trash._restore_stage_path(workspace / f"{stem}.py", digest)
    target = workspace / f"{stem}.py"
    return rescue, stage, target, digest


def _assert_source_evidence(rescue: Path) -> None:
    assert rescue.is_file()
    assert rescue.read_bytes() == PAYLOAD


def test_fault_matrix_partial_stage_write_is_never_published(tmp_path, monkeypatch):
    rescue, stage, target, _ = _fixture(tmp_path, "partial-write")
    real_write = trash.os.write
    calls = 0

    def fail_after_partial(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:5])
        raise OSError("injected crash-window write failure")

    monkeypatch.setattr(trash.os, "write", fail_after_partial)
    result = trash._restore_rescue_impl(tmp_path, rescue.name)

    assert result["status"] == "incomplete"
    assert not target.exists()
    assert stage.read_bytes() == PAYLOAD[:5]
    _assert_source_evidence(rescue)

    monkeypatch.undo()
    recovered = trash._recover_rescue_impl(tmp_path)
    assert recovered["items"][0]["status"] == "incomplete"
    assert not target.exists()
    assert stage.read_bytes() == PAYLOAD[:5]
    _assert_source_evidence(rescue)


def test_fault_matrix_stage_fsync_failure_is_recoverable(tmp_path, monkeypatch):
    rescue, stage, target, _ = _fixture(tmp_path, "stage-fsync")
    monkeypatch.setattr(
        trash.os, "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("injected file fsync failure")),
    )

    result = trash._restore_rescue_impl(tmp_path, rescue.name)
    assert result["status"] == "incomplete"
    assert not target.exists()
    assert stage.read_bytes() == PAYLOAD
    _assert_source_evidence(rescue)

    monkeypatch.undo()
    recovered = trash._recover_rescue_impl(tmp_path)
    assert recovered["items"][0]["status"] == "restored"
    assert target.read_bytes() == PAYLOAD
    assert stage.read_bytes() == PAYLOAD
    _assert_source_evidence(rescue)


def test_fault_matrix_stage_directory_sync_failure_retains_complete_stage(tmp_path, monkeypatch):
    rescue, stage, target, _ = _fixture(tmp_path, "dir-fsync")
    monkeypatch.setattr(
        trash, "_fsync_directory",
        lambda _directory: {
            "supported": True, "synced": False,
            "detail": "injected directory-entry sync failure",
        },
    )

    result = trash._restore_rescue_impl(tmp_path, rescue.name)
    assert result["status"] == "incomplete"
    assert not target.exists()
    assert stage.read_bytes() == PAYLOAD
    _assert_source_evidence(rescue)

    monkeypatch.undo()
    recovered = trash._recover_rescue_impl(tmp_path)
    assert recovered["items"][0]["status"] == "restored"
    assert target.read_bytes() == PAYLOAD
    _assert_source_evidence(rescue)


def test_fault_matrix_publish_failure_can_be_retried_without_overwrite(tmp_path, monkeypatch):
    rescue, stage, target, _ = _fixture(tmp_path, "publish")
    real_link = trash.os.link

    def fail_stage_publish(source, destination, *args, **kwargs):
        if Path(source) == stage and Path(destination) == target:
            raise OSError("injected atomic publish failure")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(trash.os, "link", fail_stage_publish)
    result = trash._restore_rescue_impl(tmp_path, rescue.name)
    assert result["status"] == "staged"
    assert not target.exists()
    assert stage.read_bytes() == PAYLOAD
    _assert_source_evidence(rescue)

    monkeypatch.undo()
    recovered = trash._recover_rescue_impl(tmp_path)
    assert recovered["items"][0]["status"] == "restored"
    assert target.read_bytes() == PAYLOAD
    assert stage.read_bytes() == PAYLOAD
    _assert_source_evidence(rescue)


def test_fault_matrix_post_publish_sync_failure_preserves_all_evidence(tmp_path, monkeypatch):
    rescue, stage, target, _ = _fixture(tmp_path, "post-publish")
    real_sync = trash._fsync_directory
    calls = 0

    def fail_second_directory_sync(directory):
        nonlocal calls
        calls += 1
        if calls == 1:  # stage directory barrier
            return real_sync(directory)
        return {
            "supported": True, "synced": False,
            "detail": "injected post-publication directory sync failure",
        }

    monkeypatch.setattr(trash, "_fsync_directory", fail_second_directory_sync)
    result = trash._restore_rescue_impl(tmp_path, rescue.name)
    assert result["status"] == "published-not-durable"
    assert target.read_bytes() == PAYLOAD
    assert stage.read_bytes() == PAYLOAD
    _assert_source_evidence(rescue)

    monkeypatch.undo()
    recovered = trash._recover_rescue_impl(tmp_path)
    assert recovered["items"][0]["status"] == "already-published"
    assert target.read_bytes() == PAYLOAD
    assert stage.read_bytes() == PAYLOAD
    _assert_source_evidence(rescue)


def test_fault_matrix_cleanup_unlink_failure_quarantines_instead_of_losing_evidence(tmp_path, monkeypatch):
    rescue, stage, target, _ = _fixture(tmp_path, "cleanup")
    restored = trash._restore_rescue_impl(tmp_path, rescue.name)
    assert restored["status"] == "restored"
    assert target.read_bytes() == PAYLOAD

    real_unlink = trash.os.unlink

    def fail_quarantine_unlink(path, *args, **kwargs):
        if ".jinlin-clean-" in Path(path).name:
            raise OSError("injected cleanup unlink failure")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(trash.os, "unlink", fail_quarantine_unlink)
    result = trash._clean_rescue_impl(tmp_path, apply=True)
    assert result["failed"]
    assert target.read_bytes() == PAYLOAD

    quarantines = list(tmp_path.glob(".*.jinlin-clean-*"))
    assert quarantines, "failed unlink must leave a named quarantine artifact"
    assert any(path.read_bytes() == PAYLOAD for path in quarantines)
    monkeypatch.undo()

    inspection = trash.inspect_rescue(tmp_path)
    assert inspection["status"] == "attention-required"
    assert inspection["counts"]["quarantined"] >= 1
    assert target.read_bytes() == PAYLOAD


def test_fault_matrix_target_race_never_overwrites_external_file(tmp_path, monkeypatch):
    rescue, stage, target, _ = _fixture(tmp_path, "target-race")
    real_link = trash.os.link
    external = b"external writer owns this path"

    def create_target_then_report_exists(source, destination, *args, **kwargs):
        if Path(source) == stage and Path(destination) == target:
            target.write_bytes(external)
            raise FileExistsError(str(target))
        return real_link(source, destination, *args, **kwargs)

    # Seed a verified stage so the test reaches the no-clobber publish boundary.
    stage.write_bytes(PAYLOAD)
    monkeypatch.setattr(trash.os, "link", create_target_then_report_exists)
    recovered = trash._recover_rescue_impl(tmp_path)

    assert recovered["items"][0]["status"] == "conflict"
    assert target.read_bytes() == external
    assert stage.read_bytes() == PAYLOAD
    _assert_source_evidence(rescue)
