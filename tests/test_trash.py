"""Trash lifecycle: manifest-proven cleanup for jinlin rollback/txn leftovers.

Phase-2 ownership principle: a leftover file is removable only when its name
matches the strict trash pattern AND the trash manifest (written by the
transaction that created it) records it. Pattern-matched names without
manifest proof are reported, never removed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jinlin_agent.config import load_config
from jinlin_agent.locks import SessionLock
from jinlin_agent.sandbox.null import NullSandbox
from jinlin_agent.state import SessionState
from jinlin_agent.tools.base import ToolContext
from jinlin_agent.tools.patch import atomic_write
from jinlin_agent.trash import MANIFEST_REL, clean_trash, record_trash, scan_trash


def sha(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def make_ctx(tmp_path: Path) -> ToolContext:
    cfg = load_config(tmp_path, home=tmp_path / "home")
    return ToolContext(
        workspace=tmp_path,
        config=cfg,
        sandbox=NullSandbox(str(tmp_path)),
        artifacts=None,
        state=SessionState(session_id="s", workspace=str(tmp_path)),
    )


def _mk_trash(ws: Path, name: str, content: bytes = b"x\n") -> Path:
    p = ws / name
    p.write_bytes(content)
    return p


def test_scan_requires_manifest_proof(tmp_path):
    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven.name, "a.py", "rollback")
    stranger = _mk_trash(tmp_path, ".b.py.jinlin-txn-cafebabe")
    provable, unrecognized = scan_trash(tmp_path)
    assert [e["name"] for e in provable] == [proven.name], (
        "manifest membership is what proves ownership, not the name pattern"
    )
    assert [e["name"] for e in unrecognized] == [stranger.name]


def test_clean_dry_run_is_the_default(tmp_path):
    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven.name, "a.py", "rollback")
    stranger = _mk_trash(tmp_path, ".b.py.jinlin-txn-cafebabe")
    report = clean_trash(tmp_path)
    assert report["status"] == "dry-run"
    assert report["removable"] == [proven.name]
    assert report["unrecognized"] == [stranger.name]
    assert proven.exists() and stranger.exists(), "dry run must not delete anything"


def test_clean_apply_removes_proven_and_keeps_unrecognized(tmp_path):
    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven.name, "a.py", "rollback")
    stranger = _mk_trash(tmp_path, ".b.py.jinlin-txn-cafebabe")
    report = clean_trash(tmp_path, apply=True)
    assert report["status"] == "applied"
    assert report["removed"] == [proven.name]
    assert not proven.exists()
    assert stranger.exists(), "unrecognized files are report-only, never removed"
    provable, unrecognized = scan_trash(tmp_path)
    assert provable == [], "the manifest entry must go with the file (repeatable runs)"
    assert [e["name"] for e in unrecognized] == [stranger.name]


def test_clean_failure_keeps_manifest_entry_and_is_repeatable(tmp_path, monkeypatch):
    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven.name, "a.py", "rollback")
    real_unlink = os.unlink

    def failing_unlink(path, *args, **kwargs):
        if Path(path) == proven:
            raise OSError("simulated locked file")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("jinlin_agent.trash.os.unlink", failing_unlink)
    report = clean_trash(tmp_path, apply=True)
    assert report["failed"] == [{"name": proven.name, "error": "simulated locked file"}]
    assert proven.exists(), "a failed removal keeps the file"
    monkeypatch.undo()
    report2 = clean_trash(tmp_path, apply=True)
    assert report2["removed"] == [proven.name], "cleanup must be repeatable after a failure"


def test_clean_refuses_while_workspace_lock_is_held(tmp_path):
    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven.name, "a.py", "rollback")
    blocker = SessionLock(tmp_path / ".jinlin" / "write.lock")
    blocker.acquire()
    try:
        report = clean_trash(tmp_path, apply=True)
    finally:
        blocker.release()
    assert report["status"] == "locked"
    assert proven.exists(), "cleanup must not race an active jinlin session"


def test_scan_tolerates_torn_manifest_tail(tmp_path):
    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven.name, "a.py", "rollback")
    with open(tmp_path / MANIFEST_REL, "ab") as fh:
        fh.write(b'{"name": ".tok')  # crash during a manifest append
    provable, _unrecognized = scan_trash(tmp_path)
    assert [e["name"] for e in provable] == [proven.name]


def test_patch_records_trash_in_manifest(tmp_path, monkeypatch):
    """Integration: a rolled-back patch leaves its trash under manifest proof."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("old-a\n", encoding="utf-8")
    second.write_text("old-b\n", encoding="utf-8")
    ctx.state.read_hashes["a.py"] = sha(first.read_bytes())
    ctx.state.read_hashes["b.py"] = sha(second.read_bytes())
    real_atomic = patch_mod.atomic_write
    calls = {"n": 0}

    def failing_second(path, data, mode_from=None, expected_sha256=None, create=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_atomic(path, data, mode_from=mode_from, expected_sha256=expected_sha256, create=create)
        raise patch_mod.AgentError("PATCH_CONFLICT", "simulated conflict", source="edit")

    monkeypatch.setattr(patch_mod, "atomic_write", failing_second)
    patch = (
        "*** Begin Patch\n"
        "*** Update File: a.py\n"
        "@@\n"
        "-old-a\n"
        "+new-a\n"
        "*** Update File: b.py\n"
        "@@\n"
        "-old-b\n"
        "+new-b\n"
        "*** End Patch"
    )
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    trashes = list(tmp_path.glob(".a.py.jinlin-rollback-*"))
    assert len(trashes) == 1, "plan A keeps the redundant trash on disk"
    manifest = json.loads((tmp_path / MANIFEST_REL).read_text(encoding="utf-8").splitlines()[0])
    assert manifest["name"] == trashes[0].name
    assert manifest["orig"] == "a.py"
    # the recorded trash is provable and removable by the controlled cleanup
    report = clean_trash(tmp_path, apply=True)
    assert report["removed"] == [trashes[0].name]
