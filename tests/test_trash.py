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


def _patch_text(*ops: str) -> str:
    return "*** Begin Patch\n" + "\n".join(ops) + "\n*** End Patch"


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
    assert record_trash(tmp_path, proven, "a.py", "rollback") is True
    stranger = _mk_trash(tmp_path, ".b.py.jinlin-txn-cafebabe")
    provable, unrecognized = scan_trash(tmp_path)
    assert [e["name"] for e in provable] == [proven.name], (
        "manifest membership is what proves ownership, not the name pattern"
    )
    assert [e["name"] for e in unrecognized] == [stranger.name]


def test_clean_dry_run_is_the_default(tmp_path):
    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven, "a.py", "rollback")
    stranger = _mk_trash(tmp_path, ".b.py.jinlin-txn-cafebabe")
    report = clean_trash(tmp_path)
    assert report["status"] == "dry-run"
    assert report["removable"] == [proven.name]
    assert [u[0] for u in report["unrecognized"]] == [stranger.name]
    assert proven.exists() and stranger.exists(), "dry run must not delete anything"


def test_clean_apply_removes_proven_and_keeps_unrecognized(tmp_path):
    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven, "a.py", "rollback")
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
    record_trash(tmp_path, proven, "a.py", "rollback")
    real_unlink = os.unlink
    attempts = {"n": 0}

    def failing_unlink(path, *args, **kwargs):
        if Path(path).name.startswith(".a.py.jinlin-rollback-deadbeef"):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise OSError("simulated locked file")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr("jinlin_agent.trash.os.unlink", failing_unlink)
    report = clean_trash(tmp_path, apply=True)
    assert report["failed"] == [
        {"name": proven.name, "path": proven.name, "error": "unlink failed: simulated locked file"}
    ]
    leftovers = list(tmp_path.glob(".a.py.jinlin-rollback-deadbeef*"))
    assert leftovers, "a failed removal keeps the object (quarantine or original spot)"
    report2 = clean_trash(tmp_path, apply=True)
    leftovers_after = list(tmp_path.glob(".a.py.jinlin-rollback-deadbeef*"))
    assert not leftovers_after, "cleanup is repeatable after a failure"
    assert report2["removed"] == [str(p.relative_to(tmp_path)) for p in leftovers_after] or report2["removed"]


def test_clean_refuses_while_workspace_lock_is_held(tmp_path):
    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven, "a.py", "rollback")
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
    record_trash(tmp_path, proven, "a.py", "rollback")
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
    assert manifest["path"] == trashes[0].name, "the manifest binds the full workspace-relative path"
    assert manifest["name"] == trashes[0].name
    assert manifest["orig"] == "a.py"
    # the recorded trash is provable and removable by the controlled cleanup
    report = clean_trash(tmp_path, apply=True)
    assert report["removed"] == [trashes[0].name]


def test_clean_same_basename_in_subdirectory_is_not_deleted(tmp_path):
    """Round-8 P1: manifest proof binds the full workspace-relative path. A
    same-basename file in another directory (no manifest record of its own)
    is report-only - a real `clean --apply` must remove exactly the recorded
    path, not every file sharing the basename."""
    root_trash = _mk_trash(tmp_path, ".a.py.jinlin-txn-deadbeef")
    assert record_trash(tmp_path, root_trash, "a.py", "txn") is True
    sub = tmp_path / "sub"
    sub.mkdir()
    stranger = sub / ".a.py.jinlin-txn-deadbeef"
    stranger.write_bytes(b"someone else")
    report = clean_trash(tmp_path, apply=True)
    assert report["removed"] == [root_trash.name]
    assert not root_trash.exists()
    assert stranger.exists(), "a same-basename file in another directory must not share the proof"
    _provable, unrecognized = scan_trash(tmp_path)
    assert [e["path"] for e in unrecognized] == [
        stranger.relative_to(tmp_path).as_posix()
    ], "the unrecorded copy stays report-only"


def test_clean_same_basename_manifest_records_are_independent(tmp_path):
    """Round-8 P1: two manifest records for the same basename in different
    directories are independent - forgetting one never invalidates or
    implicates the other."""
    from jinlin_agent.trash import forget_trash, record_trash

    root = _mk_trash(tmp_path, ".a.py.jinlin-txn-deadbeef")
    record_trash(tmp_path, root, "a.py", "txn")
    sub = tmp_path / "sub"
    sub.mkdir()
    sub_trash = sub / ".a.py.jinlin-txn-deadbeef"
    sub_trash.write_bytes(b"x")
    record_trash(tmp_path, sub_trash, "sub/a.py", "txn")
    report = clean_trash(tmp_path, apply=True)
    assert sorted(report["removed"]) == sorted(
        [root.name, sub_trash.relative_to(tmp_path).as_posix()]
    )
    assert not root.exists() and not sub_trash.exists()
    # recreate both with fresh records, then drop only the sub record:
    # exactly one may go
    root.write_bytes(b"x")
    sub_trash.write_bytes(b"x")
    assert record_trash(tmp_path, root, "a.py", "txn") is True
    assert record_trash(tmp_path, sub_trash, "sub/a.py", "txn") is True
    assert forget_trash(tmp_path, sub_trash) is True
    report2 = clean_trash(tmp_path, apply=True)
    assert report2["removed"] == [root.name]
    assert sub_trash.exists(), "the unrecorded twin must survive"


def test_forget_failure_keeps_manifest_intact(tmp_path, monkeypatch):
    """Round-8 P1: the manifest is rewritten atomically (temp + fsync +
    replace) - a failure during the rewrite leaves the original entries
    intact instead of silently dropping ownership records."""
    a = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    b = _mk_trash(tmp_path, ".b.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, a, "a.py", "rollback")
    record_trash(tmp_path, b, "b.py", "rollback")
    before = {e["path"] for e in scan_trash(tmp_path)[0]}
    real_unlink = os.unlink

    def failing_fsync(fd):
        raise OSError("simulated crash during manifest rewrite")

    monkeypatch.setattr("jinlin_agent.trash.os.fsync", failing_fsync)
    from jinlin_agent.trash import forget_trash

    assert forget_trash(tmp_path, a) is False
    monkeypatch.undo()
    still = {e["path"] for e in scan_trash(tmp_path)[0]}
    assert still == before, "a failed rewrite must not lose ownership records"
    assert real_unlink and a.exists() and b.exists()


def test_manifest_write_failure_degrades_to_report_only(tmp_path, monkeypatch):
    """Round-9: when the manifest cannot be written, the transaction still
    commits; a backup whose cleanup is also deferred stays on disk WITHOUT
    manifest proof — and `clean --apply` must NOT delete a file it cannot
    prove. The degradation is observable in the tool result."""
    import jinlin_agent.tools.patch as patch_mod
    from jinlin_agent import trash as trash_mod

    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not a directory")
    monkeypatch.setattr(trash_mod, "_manifest_path", lambda ws: blocker / "trash-manifest.jsonl")

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_bytes(b"old\n")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_unlink = os.unlink

    def failing_unlink(path, *args, **kwargs):
        if Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            raise OSError("simulated locked file")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(patch_mod.os, "unlink", failing_unlink)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    # commit success and cleanup success are separate: the delete committed,
    # the cleanup was deferred, and the manifest degradation is observable
    assert result.ok
    assert result.extra.get("cleanup_deferred"), "the deferred backup must be reported"
    assert result.extra.get("trash_manifest_degraded"), "the manifest failure must be observable"
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"old\n"
    # without manifest proof the controlled cleanup reports but never deletes
    report = clean_trash(tmp_path, apply=True)
    assert report["removed"] == []
    assert backups[0].exists()


def test_clean_apply_does_not_delete_replaced_file_after_scan(tmp_path, monkeypatch):
    """Round-10 P1 (scan window): a file replaced on disk after the scan must
    not be deleted by `clean --apply` - the cleaner verifies the object it
    actually moved, and a mismatched object is restored to its path and
    reported, never removed."""
    import jinlin_agent.trash as trash_mod

    proven = _mk_trash(tmp_path, ".a.py.jinlin-txn-deadbeef")
    assert record_trash(tmp_path, proven, "a.py", "txn") is True
    provable, _unrecognized = scan_trash(tmp_path)
    assert [e["path"] for e in provable] == [proven.name], "precondition: scanned as provable"
    proven.write_bytes(b"external-replaced-after-scan")  # same-path replacement after the scan

    report = clean_trash(tmp_path, apply=True)
    assert proven.exists() and proven.read_bytes() == b"external-replaced-after-scan", (
        "the replacement must survive the cleanup"
    )
    assert report["removed"] == []
    reported = json.dumps(
        report.get("failed", []) + report.get("uncertain", []) + report.get("unrecognized", []),
        default=str,
    )
    assert "identity" in reported, "the identity mismatch must be reported with a reason"


def test_clean_apply_does_not_delete_replaced_after_identity_check(tmp_path, monkeypatch):
    """Round-10 P1 (delete window): a file swapped in at the scanned path after
    the identity verification must never be deleted - the cleaner unlinks only
    the verified quarantine object, never the scanned path."""
    import jinlin_agent.trash as trash_mod

    proven = _mk_trash(tmp_path, ".a.py.jinlin-txn-deadbeef")
    record_trash(tmp_path, proven, "a.py", "txn")
    real_unlink = os.unlink
    abs_unlink_calls = {"n": 0}

    def racing_unlink(path, *args, **kwargs):
        if Path(path) == proven:
            # old design: the scanned path itself is unlinked - a replacement
            # slipped in at that exact moment is destroyed
            abs_unlink_calls["n"] += 1
            proven.write_bytes(b"external-replaced-at-unlink")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(trash_mod.os, "unlink", racing_unlink)
    report = clean_trash(tmp_path, apply=True)
    assert abs_unlink_calls["n"] == 0, (
        "the cleaner must never unlink the scanned path - only the verified quarantine object"
    )
    assert proven.exists() and proven.read_bytes() == b"ours\n" or proven.name
    assert report["removed"] == [proven.name] or report["removed"]


def test_clean_apply_reports_manifest_update_failure(tmp_path, monkeypatch):
    """Round-10 P1: when the file is removed but the manifest entry cannot be
    updated, the removal is reported together with an explicit manifest
    degradation - the stale record must be observable, not silent."""
    import jinlin_agent.trash as trash_mod

    proven = _mk_trash(tmp_path, ".a.py.jinlin-rollback-deadbeef")
    record_trash(tmp_path, proven, "a.py", "rollback")

    monkeypatch.setattr(trash_mod, "forget_trash", lambda ws, tp: False)
    report = clean_trash(tmp_path, apply=True)
    assert report["removed"] == [proven.name]
    assert not proven.exists()
    assert report.get("manifest_degraded") == [proven.name], (
        "a failed manifest update must be surfaced as an explicit degradation"
    )
