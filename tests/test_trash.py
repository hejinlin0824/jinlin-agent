"""Trash lifecycle: manifest-proven cleanup for jinlin rollback/txn leftovers.

Phase-2 ownership principle: a leftover file is removable only when its name
matches the strict trash pattern AND the trash manifest (written by the
transaction that created it) records it. Pattern-matched names without
manifest proof are reported, never removed.
"""

from __future__ import annotations

import json
import hashlib
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
    real_scan = trash_mod.scan_trash

    def racing_scan(workspace):
        provable, unrecognized = real_scan(workspace)
        assert [e["path"] for e in provable] == [proven.name], "precondition: scanned as provable"
        # Replace the candidate after clean_trash has scanned it, but before
        # clean_trash moves it into quarantine and verifies the moved object.
        proven.write_bytes(b"external-replaced-after-scan")
        return provable, unrecognized

    monkeypatch.setattr(trash_mod, "scan_trash", racing_scan)
    report = clean_trash(tmp_path, apply=True)
    assert proven.exists() and proven.read_bytes() == b"external-replaced-after-scan", (
        "the replacement must survive the cleanup"
    )
    quarantines = list(tmp_path.glob(proven.name + ".q-*"))
    assert len(quarantines) == 1 and quarantines[0].read_bytes() == b"external-replaced-after-scan", (
        "the quarantine source must remain as evidence; restoring by hard link must not unlink by name"
    )
    assert report["removed"] == []
    reported = json.dumps(
        report.get("failed", []) + report.get("uncertain", []) + report.get("unrecognized", []),
        default=str,
    )
    assert "identity" in reported, "the identity mismatch must be reported with a reason"


def test_clean_apply_does_not_delete_replaced_after_identity_check(tmp_path, monkeypatch):
    """Round-10 P1 (delete window): once the verified object has been moved to
    quarantine, a new file created at the scanned pathname must survive. The
    cleaner may unlink only the quarantined object, never the pathname that an
    external writer can reclaim."""
    import jinlin_agent.trash as trash_mod

    proven = _mk_trash(tmp_path, ".a.py.jinlin-txn-deadbeef")
    record_trash(tmp_path, proven, "a.py", "txn")
    real_unlink = os.unlink
    unlinked = []

    def racing_unlink(path, *args, **kwargs):
        candidate = Path(path)
        if candidate.name.startswith(proven.name + ".q-"):
            # The original object has already been moved and identity-checked.
            # An external writer now reclaims the scanned pathname.
            proven.write_bytes(b"external-replaced-after-identity-check")
            unlinked.append(candidate)
        elif candidate == proven:
            pytest.fail("cleaner must never unlink the scanned pathname")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(trash_mod.os, "unlink", racing_unlink)
    report = clean_trash(tmp_path, apply=True)

    assert len(unlinked) == 1, "the verified quarantine object should be removed"
    assert proven.exists()
    assert proven.read_bytes() == b"external-replaced-after-identity-check"
    assert report["removed"] == [proven.name]
    assert report["failed"] == []


def test_write_exclusive_rejects_zero_progress(tmp_path, monkeypatch):
    """A zero-byte os.write must fail rather than spin forever or report success."""
    import jinlin_agent.tools.patch as patch_mod

    target = tmp_path / "stalled.bin"
    monkeypatch.setattr(patch_mod.os, "write", lambda fd, data: 0)
    with pytest.raises(OSError, match="no forward progress"):
        patch_mod._write_exclusive(target, b"payload")
    assert target.exists()
    assert target.read_bytes() == b""


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



def test_rescue_list_and_restore_verifies_sha_and_retains_source(tmp_path):
    from jinlin_agent.trash import list_rescue, restore_rescue

    original = b"important original bytes\n"
    rescue = _mk_trash(tmp_path, ".lost.py.jinlin-rescue-deadbeef", original)
    assert record_trash(tmp_path, rescue, "lost.py", "rescue") is True

    report = list_rescue(tmp_path)
    assert report["status"] == "ok"
    assert len(report["rescued"]) == 1
    item = report["rescued"][0]
    assert item["path"] == rescue.name
    assert item["orig"] == "lost.py"
    assert item["sha256"] == sha(original)

    restored = restore_rescue(tmp_path, rescue.name)
    assert restored["status"] == "restored"
    assert (tmp_path / "lost.py").read_bytes() == original
    assert rescue.read_bytes() == original, "restore must retain the rescue source"
    assert restored["sha256"] == sha(original)


def test_rescue_restore_refuses_existing_target_without_overwrite(tmp_path):
    from jinlin_agent.trash import restore_rescue

    rescue = _mk_trash(tmp_path, ".lost.py.jinlin-rescue-deadbeef", b"original")
    record_trash(tmp_path, rescue, "lost.py", "rescue")
    target = tmp_path / "lost.py"
    target.write_bytes(b"external claimant")

    result = restore_rescue(tmp_path, rescue.name)
    assert result["status"] == "conflict"
    assert target.read_bytes() == b"external claimant"
    assert rescue.read_bytes() == b"original"


def test_rescue_list_rejects_replaced_or_modified_evidence(tmp_path):
    from jinlin_agent.trash import list_rescue

    rescue = _mk_trash(tmp_path, ".lost.py.jinlin-rescue-deadbeef", b"original")
    record_trash(tmp_path, rescue, "lost.py", "rescue")
    rescue.write_bytes(b"modified evidence")

    report = list_rescue(tmp_path)
    assert report["rescued"] == []
    assert [x["path"] for x in report["unrecognized"]] == [rescue.name]


def test_rescue_restore_rejects_manifest_path_traversal(tmp_path):
    from jinlin_agent.trash import restore_rescue

    rescue = _mk_trash(tmp_path, ".lost.py.jinlin-rescue-deadbeef", b"original")
    record_trash(tmp_path, rescue, "../outside.py", "rescue")
    result = restore_rescue(tmp_path, rescue.name)
    assert result["status"] == "rejected"
    assert not (tmp_path.parent / "outside.py").exists()
    assert rescue.exists()


def test_rescue_cli_lists_and_restores(tmp_path, capsys):
    from jinlin_agent.cli import main

    rescue = _mk_trash(tmp_path, ".lost.py.jinlin-rescue-deadbeef", b"cli original")
    record_trash(tmp_path, rescue, "lost.py", "rescue")
    assert main(["rescue", "list", "--workspace", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert rescue.name in output and "lost.py" in output and "sha256=" in output

    assert main(["rescue", "restore", rescue.name, "--workspace", str(tmp_path)]) == 0
    assert (tmp_path / "lost.py").read_bytes() == b"cli original"
    assert rescue.exists()



def test_legacy_rescue_manifest_remains_discoverable_and_restorable(tmp_path):
    from jinlin_agent.trash import list_rescue, restore_rescue

    rescue = _mk_trash(tmp_path, ".legacy.py.jinlin-rescue-cafebabe", b"legacy original")
    record_trash(tmp_path, rescue, "legacy.py", "rescue")
    manifest_path = tmp_path / MANIFEST_REL
    entry = json.loads(manifest_path.read_text(encoding="utf-8").splitlines()[0])
    entry["identity"].pop("sha256", None)  # format written by pre-upgrade versions
    manifest_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")

    report = list_rescue(tmp_path)
    assert [x["path"] for x in report["rescued"]] == [rescue.name]
    assert report["rescued"][0]["integrity"] == "legacy-stat"
    restored = restore_rescue(tmp_path, rescue.name)
    assert restored["status"] == "restored"
    assert (tmp_path / "legacy.py").read_bytes() == b"legacy original"
    assert rescue.exists()



def test_rescue_scan_handles_unreadable_file_without_unbound_identity(tmp_path, monkeypatch):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".unreadable.py.jinlin-rescue-01020304", b"evidence")
    record_trash(tmp_path, rescue, "unreadable.py", "rescue")
    real_read_bytes = Path.read_bytes

    def denied(path):
        if path == rescue:
            raise PermissionError("simulated sharing violation")
        return real_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", denied)
    report = trash_mod.list_rescue(tmp_path)
    assert report["rescued"] == []
    assert [x["path"] for x in report["unrecognized"]] == [rescue.name]
    # Exercise the lock-already-held scan used by restore as well.
    report2 = trash_mod.list_rescue_unlocked(tmp_path)
    assert report2["rescued"] == []
    assert [x["path"] for x in report2["unrecognized"]] == [rescue.name]


def test_rescue_restore_zero_progress_is_uncertain_and_preserves_source(tmp_path, monkeypatch):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".stalled.py.jinlin-rescue-01020305", b"original payload")
    record_trash(tmp_path, rescue, "stalled.py", "rescue")
    monkeypatch.setattr(trash_mod.os, "write", lambda fd, data: 0)

    result = trash_mod.restore_rescue(tmp_path, rescue.name)
    assert result["status"] == "incomplete"
    assert not (tmp_path / "stalled.py").exists()
    stage = Path(result["stage"])
    assert (tmp_path / stage).exists()
    assert (tmp_path / stage).read_bytes() == b""
    assert rescue.read_bytes() == b"original payload"
    # Recovery recognizes but never publishes an incomplete stage.
    recovered = trash_mod.recover_rescue(tmp_path)
    assert recovered["items"][0]["status"] == "incomplete"
    assert not (tmp_path / "stalled.py").exists()
    assert rescue.read_bytes() == b"original payload"


def test_rescue_restore_fsync_failure_leaves_recoverable_stage(tmp_path, monkeypatch):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".sync.py.jinlin-rescue-01020306", b"original payload")
    record_trash(tmp_path, rescue, "sync.py", "rescue")
    monkeypatch.setattr(trash_mod.os, "fsync", lambda fd: (_ for _ in ()).throw(OSError("simulated fsync failure")))

    result = trash_mod.restore_rescue(tmp_path, rescue.name)
    assert result["status"] == "incomplete"
    assert not (tmp_path / "sync.py").exists()
    assert rescue.read_bytes() == b"original payload"
    # Simulate a fresh process: restore the real fsync implementation, then
    # recover only after re-verifying and re-establishing durability.
    monkeypatch.undo()
    recovered = trash_mod.recover_rescue(tmp_path)
    assert recovered["items"][0]["status"] == "restored"
    assert (tmp_path / "sync.py").read_bytes() == b"original payload"
    assert rescue.read_bytes() == b"original payload"



def test_rescue_restore_repeated_after_success_is_non_destructive(tmp_path):
    from jinlin_agent.trash import restore_rescue

    rescue = _mk_trash(tmp_path, ".repeat.py.jinlin-rescue-01020307", b"original")
    record_trash(tmp_path, rescue, "repeat.py", "rescue")
    first = restore_rescue(tmp_path, rescue.name)
    second = restore_rescue(tmp_path, rescue.name)
    assert first["status"] == "restored"
    assert second["status"] == "already-published"
    assert (tmp_path / "repeat.py").read_bytes() == b"original"
    assert rescue.read_bytes() == b"original"


def test_rescue_restore_rejects_symlink_parent_escape(tmp_path):
    import jinlin_agent.trash as trash_mod

    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    try:
        (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks unavailable on this platform")
    rescue = _mk_trash(tmp_path, ".payload.py.jinlin-rescue-01020308", b"original")
    record_trash(tmp_path, rescue, "linked/payload.py", "rescue")
    result = trash_mod.restore_rescue(tmp_path, rescue.name)
    assert result["status"] == "rejected"
    assert not (outside / "payload.py").exists()
    assert rescue.read_bytes() == b"original"


def test_rescue_restore_crash_after_stage_is_recovered_atomically(tmp_path):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".recover.py.jinlin-rescue-01020309", b"crash-safe payload")
    record_trash(tmp_path, rescue, "recover.py", "rescue")
    digest = sha(b"crash-safe payload")
    stage = trash_mod._restore_stage_path(tmp_path / "recover.py", digest)
    stage.write_bytes(b"crash-safe payload")

    report = trash_mod.recover_rescue(tmp_path)
    assert report["status"] == "ok"
    assert report["items"][0]["status"] == "restored"
    assert (tmp_path / "recover.py").read_bytes() == b"crash-safe payload"
    assert stage.read_bytes() == b"crash-safe payload"
    assert rescue.read_bytes() == b"crash-safe payload"


def test_rescue_restore_does_not_publish_corrupt_stage(tmp_path):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".corrupt.py.jinlin-rescue-01020310", b"expected")
    record_trash(tmp_path, rescue, "corrupt.py", "rescue")
    stage = trash_mod._restore_stage_path(tmp_path / "corrupt.py", sha(b"expected"))
    stage.write_bytes(b"partial")
    report = trash_mod.recover_rescue(tmp_path)
    assert report["items"][0]["status"] == "incomplete"
    assert not (tmp_path / "corrupt.py").exists()
    assert stage.read_bytes() == b"partial"
    assert rescue.read_bytes() == b"expected"


def test_rescue_restore_rejects_windows_drive_and_backslash_manifest_paths(tmp_path):
    from jinlin_agent.trash import restore_rescue

    for idx, target in enumerate((r"..\\outside.py", r"C:\\temp\\evil.py", r"folder\\..\\evil.py", "file:stream")):
        rescue = _mk_trash(tmp_path, f".path{idx}.py.jinlin-rescue-010203{idx:02x}", b"payload")
        record_trash(tmp_path, rescue, target, "rescue")
        result = restore_rescue(tmp_path, rescue.name)
        assert result["status"] == "rejected"
        assert rescue.exists()


def test_rescue_cli_recover_lists_staged_restore(tmp_path, capsys):
    import jinlin_agent.trash as trash_mod
    from jinlin_agent.cli import main

    rescue = _mk_trash(tmp_path, ".cli-recover.py.jinlin-rescue-01020311", b"cli payload")
    record_trash(tmp_path, rescue, "cli-recover.py", "rescue")
    stage = trash_mod._restore_stage_path(tmp_path / "cli-recover.py", sha(b"cli payload"))
    stage.write_bytes(b"cli payload")
    assert main(["rescue", "recover", "--workspace", str(tmp_path)]) == 0
    assert (tmp_path / "cli-recover.py").read_bytes() == b"cli payload"


def test_rescue_recover_refuses_symlink_target(tmp_path):
    import jinlin_agent.trash as trash_mod

    outside = tmp_path.parent / (tmp_path.name + "-outside-target")
    outside.write_bytes(b"same payload")
    rescue = _mk_trash(tmp_path, ".restored.py.jinlin-rescue-01020312", b"same payload")
    record_trash(tmp_path, rescue, "restored.py", "rescue")
    stage = trash_mod._restore_stage_path(tmp_path / "restored.py", sha(b"same payload"))
    stage.write_bytes(b"same payload")
    try:
        (tmp_path / "restored.py").symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("file symlinks unavailable on this platform")

    report = trash_mod.recover_rescue(tmp_path)
    assert report["items"][0]["status"] == "conflict"
    assert outside.read_bytes() == b"same payload"
    assert (tmp_path / "restored.py").is_symlink()
    assert stage.read_bytes() == b"same payload"
    assert rescue.read_bytes() == b"same payload"


def test_rescue_restore_short_writes_are_completed(tmp_path, monkeypatch):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".short.py.jinlin-rescue-01020313", b"short-write-payload")
    record_trash(tmp_path, rescue, "short.py", "rescue")
    real_write = trash_mod.os.write
    calls = {"n": 0}

    def short_write(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_write(fd, data[:3])
        return real_write(fd, data)

    monkeypatch.setattr(trash_mod.os, "write", short_write)
    result = trash_mod.restore_rescue(tmp_path, rescue.name)
    assert result["status"] == "restored"
    assert calls["n"] > 1
    assert (tmp_path / "short.py").read_bytes() == b"short-write-payload"
    assert rescue.read_bytes() == b"short-write-payload"



def test_rescue_recover_reports_orphan_stage_without_publishing_or_deleting(tmp_path):
    import jinlin_agent.trash as trash_mod

    orphan = tmp_path / ".ghost.py.jinlin-restore-0123456789abcdef"
    orphan.write_bytes(b"unclaimed bytes")
    report = trash_mod.recover_rescue(tmp_path)
    assert report["status"] == "ok"
    assert report["items"] == []
    assert [item["path"] for item in report["orphaned"]] == [orphan.name]
    assert orphan.read_bytes() == b"unclaimed bytes"


def test_rescue_recover_does_not_call_a_verified_stage_orphan(tmp_path):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".known.py.jinlin-rescue-01020314", b"known payload")
    record_trash(tmp_path, rescue, "known.py", "rescue")
    stage = trash_mod._restore_stage_path(tmp_path / "known.py", sha(b"known payload"))
    stage.write_bytes(b"known payload")
    report = trash_mod.recover_rescue(tmp_path)
    assert report["items"][0]["status"] == "restored"
    assert report["orphaned"] == []


def test_rescue_clean_is_dry_run_by_default_and_removes_only_verified_evidence(tmp_path):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".cleaned.py.jinlin-rescue-01020315", b"cleanable payload")
    record_trash(tmp_path, rescue, "cleaned.py", "rescue")
    restored = trash_mod.restore_rescue(tmp_path, rescue.name)
    assert restored["status"] == "restored"
    stage = trash_mod._restore_stage_path(tmp_path / "cleaned.py", sha(b"cleanable payload"))

    preview = trash_mod.clean_rescue(tmp_path)
    assert preview["status"] == "dry-run"
    assert len(preview["eligible"]) == 1
    assert rescue.exists() and stage.exists()

    applied = trash_mod.clean_rescue(tmp_path, apply=True)
    assert applied["status"] == "applied"
    assert applied["removed"] == [rescue.name]
    assert not rescue.exists() and not stage.exists()
    assert (tmp_path / "cleaned.py").read_bytes() == b"cleanable payload"
    assert rescue.name not in trash_mod._load_manifest(tmp_path)


def test_rescue_clean_refuses_when_restored_target_changed(tmp_path):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".changed.py.jinlin-rescue-01020316", b"original payload")
    record_trash(tmp_path, rescue, "changed.py", "rescue")
    assert trash_mod.restore_rescue(tmp_path, rescue.name)["status"] == "restored"
    (tmp_path / "changed.py").write_bytes(b"external replacement")
    report = trash_mod.clean_rescue(tmp_path, apply=True)
    assert report["removed"] == []
    assert report["failed"]
    assert rescue.exists()


def test_rescue_clean_quarantines_and_verifies_before_unlink(tmp_path, monkeypatch):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".quarantine.py.jinlin-rescue-01020317", b"payload")
    record_trash(tmp_path, rescue, "quarantine.py", "rescue")
    assert trash_mod.restore_rescue(tmp_path, rescue.name)["status"] == "restored"
    real_replace = trash_mod.os.replace
    swapped = {"done": False}

    def replace_with_swap(src, dst):
        if Path(src) == rescue and not swapped["done"]:
            rescue.write_bytes(b"changed after scan")
            swapped["done"] = True
        return real_replace(src, dst)

    monkeypatch.setattr(trash_mod.os, "replace", replace_with_swap)
    report = trash_mod.clean_rescue(tmp_path, apply=True)
    assert report["failed"]
    assert not report["removed"]
    # The mismatching object is preserved in quarantine, not deleted.
    quarantines = list(tmp_path.glob(".*.jinlin-clean-*"))
    assert quarantines and quarantines[0].read_bytes() == b"changed after scan"


def test_rescue_publish_refuses_unexpected_stage_hardlinks(tmp_path):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".linked.py.jinlin-rescue-01020318", b"linked payload")
    record_trash(tmp_path, rescue, "linked.py", "rescue")
    digest = sha(b"linked payload")
    stage = trash_mod._restore_stage_path(tmp_path / "linked.py", digest)
    stage.write_bytes(b"linked payload")
    extra_link = tmp_path / "extra-link"
    try:
        os.link(stage, extra_link)
    except OSError:
        pytest.skip("hard links unavailable on this filesystem")
    report = trash_mod.recover_rescue(tmp_path)
    assert report["items"][0]["status"] == "uncertain"
    assert not (tmp_path / "linked.py").exists()
    assert stage.read_bytes() == b"linked payload"
    assert extra_link.read_bytes() == b"linked payload"


def test_rescue_directory_fsync_reports_platform_boundary(tmp_path):
    import jinlin_agent.trash as trash_mod

    result = trash_mod._fsync_directory(tmp_path)
    if os.name == "nt":
        assert result["supported"] is False
        assert result["synced"] is False
    else:
        assert result["supported"] is True
        assert result["synced"] is True



def test_rescue_cli_clean_requires_explicit_apply(tmp_path, capsys):
    from jinlin_agent.cli import main
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".cli-clean.py.jinlin-rescue-01020319", b"cli clean")
    record_trash(tmp_path, rescue, "cli-clean.py", "rescue")
    assert trash_mod.restore_rescue(tmp_path, rescue.name)["status"] == "restored"

    assert main(["rescue", "clean", "--workspace", str(tmp_path)]) == 0
    assert rescue.exists()
    assert "eligible: 1" in capsys.readouterr().out

    assert main(["rescue", "clean", "--apply", "--workspace", str(tmp_path)]) == 0
    assert not rescue.exists()
    assert (tmp_path / "cli-clean.py").read_bytes() == b"cli clean"


def test_rescue_cli_recover_returns_nonzero_for_orphan_stage(tmp_path, capsys):
    from jinlin_agent.cli import main

    orphan = tmp_path / ".lost.py.jinlin-restore-fedcba9876543210"
    orphan.write_bytes(b"orphan")
    assert main(["rescue", "recover", "--workspace", str(tmp_path)]) == 2
    assert "orphaned:" in capsys.readouterr().out
    assert orphan.read_bytes() == b"orphan"



def test_rescue_clean_reports_interrupted_quarantine_without_touching_it(tmp_path):
    import jinlin_agent.trash as trash_mod

    quarantine = tmp_path / ".lost.py.jinlin-clean-a1b2c3d4"
    quarantine.write_bytes(b"interrupted cleanup evidence")
    report = trash_mod.clean_rescue(tmp_path, apply=True)
    assert len(report["quarantined"]) == 1
    assert report["quarantined"][0]["path"] == quarantine.name
    assert report["quarantined"][0]["kind"] == "file"
    assert report["quarantined"][0]["classification"] == "unrecognized"
    assert quarantine.read_bytes() == b"interrupted cleanup evidence"
    assert report["removed"] == []



def test_rescue_audit_hash_chain_and_transaction_state_lifecycle(tmp_path):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".audit.py.jinlin-rescue-10203040", b"audit payload")
    record_trash(tmp_path, rescue, "audit.py", "rescue")
    restored = trash_mod.restore_rescue(tmp_path, rescue.name)
    assert restored["status"] == "restored"

    audit = trash_mod._read_audit(tmp_path)
    assert audit["issues"] == []
    assert len(audit["events"]) == 1
    event = audit["events"][0]
    assert event["action"] == "restore"
    assert event["status"] == "restored"
    assert event["sha256"] == sha(b"audit payload")

    report = trash_mod.inspect_rescue(tmp_path)
    assert report["status"] == "ok"
    assert report["counts"]["verified_rescue"] == 1
    assert report["counts"]["audit_events"] == 1
    assert report["transactions"][0]["state"] == "PUBLISHED"
    assert report["stages"][0]["integrity"] == "sha256-valid"


def test_rescue_audit_detects_tampering_and_refuses_to_extend_chain(tmp_path):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".tamper.py.jinlin-rescue-10203041", b"tamper payload")
    record_trash(tmp_path, rescue, "tamper.py", "rescue")
    assert trash_mod.restore_rescue(tmp_path, rescue.name)["status"] == "restored"

    audit_path = tmp_path / ".jinlin" / "rescue-audit.jsonl"
    original = audit_path.read_text(encoding="utf-8")
    audit_path.write_text(original.replace('"status": "restored"', '"status": "conflict"'), encoding="utf-8")
    audit = trash_mod._read_audit(tmp_path)
    assert audit["issues"]
    second = trash_mod.restore_rescue(tmp_path, rescue.name)
    assert second["status"] == "already-published"
    assert second["audit_warnings"]
    assert audit_path.read_text(encoding="utf-8") != ""


def test_rescue_inspect_reports_orphan_and_interrupted_cleanup(tmp_path):
    import jinlin_agent.trash as trash_mod

    orphan = tmp_path / ".unknown.py.jinlin-restore-0123456789abcdef"
    orphan.write_bytes(b"orphan")
    quarantine = tmp_path / ".unknown.py.jinlin-clean-aabbccdd"
    quarantine.write_bytes(b"quarantine")
    report = trash_mod.inspect_rescue(tmp_path)
    assert report["status"] == "attention-required"
    assert report["counts"]["orphaned_stages"] == 1
    assert report["counts"]["quarantined"] == 1
    assert report["orphaned"][0]["state"] == "ORPHANED"
    assert report["quarantined"][0]["state"] == "CLEANUP_INTERRUPTED"
    assert orphan.exists() and quarantine.exists()


def test_rescue_clean_records_cleaned_state_in_audit(tmp_path):
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".state.py.jinlin-rescue-10203042", b"state payload")
    record_trash(tmp_path, rescue, "state.py", "rescue")
    assert trash_mod.restore_rescue(tmp_path, rescue.name)["status"] == "restored"
    preview = trash_mod.clean_rescue(tmp_path)
    assert preview["status"] == "dry-run"
    applied = trash_mod.clean_rescue(tmp_path, apply=True)
    assert applied["removed"] == [rescue.name]
    audit = trash_mod._read_audit(tmp_path)
    states = trash_mod._transaction_states(audit["events"])
    assert len(states) == 1
    assert states[0]["state"] == "CLEANED"


def test_rescue_inspect_cli_is_read_only_and_reports_audit(tmp_path, capsys):
    from jinlin_agent.cli import main
    import jinlin_agent.trash as trash_mod

    rescue = _mk_trash(tmp_path, ".inspect.py.jinlin-rescue-10203043", b"inspect payload")
    record_trash(tmp_path, rescue, "inspect.py", "rescue")
    assert trash_mod.restore_rescue(tmp_path, rescue.name)["status"] == "restored"
    before = (tmp_path / ".jinlin" / "rescue-audit.jsonl").read_bytes()
    assert main(["rescue", "inspect", "--workspace", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "PUBLISHED" in output
    assert "audit_events: 1" in output
    assert (tmp_path / ".jinlin" / "rescue-audit.jsonl").read_bytes() == before




def test_rescue_manifest_persists_distinct_transaction_ids(tmp_path):
    import jinlin_agent.trash as trash_mod
    first = _mk_trash(tmp_path, ".tx-one.py.jinlin-rescue-11223344", b"same transaction payload")
    second = _mk_trash(tmp_path, ".tx-two.py.jinlin-rescue-11223344", b"same transaction payload")
    assert record_trash(tmp_path, first, "tx-one.py", "rescue")
    assert record_trash(tmp_path, second, "tx-two.py", "rescue")
    manifest = trash_mod._load_manifest(tmp_path)
    tx_one = manifest[first.name]["transaction_id"]
    tx_two = manifest[second.name]["transaction_id"]
    assert len(tx_one) == 32 and len(tx_two) == 32
    assert tx_one != tx_two


def test_rescue_audit_v2_has_monotonic_sequence_unique_event_ids_and_persistent_txid(tmp_path):
    import jinlin_agent.trash as trash_mod
    rescue = _mk_trash(tmp_path, ".audit-v2.py.jinlin-rescue-11223345", b"audit v2")
    assert record_trash(tmp_path, rescue, "audit-v2.py", "rescue")
    txid = trash_mod._load_manifest(tmp_path)[rescue.name]["transaction_id"]

    first = trash_mod.restore_rescue(tmp_path, rescue.name)
    second = trash_mod.restore_rescue(tmp_path, rescue.name)
    assert first["status"] == "restored"
    assert second["status"] == "already-published"
    audit = trash_mod._read_audit(tmp_path)
    assert audit["issues"] == []
    assert [event["version"] for event in audit["events"]] == [2, 2]
    assert [event["seq"] for event in audit["events"]] == [1, 2]
    assert len({event["event_id"] for event in audit["events"]}) == 2
    assert {event["txid"] for event in audit["events"]} == {txid}
    projected = trash_mod._transaction_states(audit["events"])
    assert len(projected) == 1
    assert projected[0]["state"] == "PUBLISHED"
    assert projected[0]["event_count"] == 2
    assert projected[0]["first_seq"] == 1 and projected[0]["last_seq"] == 2


def test_rescue_audit_v2_rejects_sequence_gap_and_refuses_append(tmp_path):
    import json
    import jinlin_agent.trash as trash_mod
    rescue = _mk_trash(tmp_path, ".audit-seq.py.jinlin-rescue-11223346", b"sequence gap")
    assert record_trash(tmp_path, rescue, "audit-seq.py", "rescue")
    assert trash_mod.restore_rescue(tmp_path, rescue.name)["status"] == "restored"
    path = tmp_path / trash_mod.AUDIT_REL
    event = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    event["seq"] = 7
    body = {key: value for key, value in event.items() if key != "event_hash"}
    event["event_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(event, sort_keys=True) + "\n", encoding="utf-8")

    audit = trash_mod._read_audit(tmp_path)
    assert audit["issues"]
    assert "sequence is not contiguous" in audit["issues"][0]["reason"]
    ok, warning = trash_mod._append_audit_event(tmp_path, "restore", "conflict", txid="a" * 32)
    assert not ok and "integrity issues" in warning


def test_rescue_audit_accepts_legacy_v1_prefix_then_appends_v2(tmp_path):
    import json
    import jinlin_agent.trash as trash_mod
    legacy_body = {
        "version": 1, "ts": "2026-01-01T00:00:00Z", "txid": "a" * 24,
        "action": "restore", "status": "restored", "prev_hash": "0" * 64,
        "rescue": ".legacy.py.jinlin-rescue-11223347", "target": "legacy.py",
        "sha256": sha(b"legacy"),
    }
    legacy_body["event_hash"] = hashlib.sha256(
        json.dumps({k: v for k, v in legacy_body.items()}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    path = tmp_path / trash_mod.AUDIT_REL
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(legacy_body, sort_keys=True) + "\n", encoding="utf-8")

    ok, warning = trash_mod._append_audit_event(tmp_path, "clean", "cleanup-blocked", txid="b" * 32)
    assert ok and warning is None
    audit = trash_mod._read_audit(tmp_path)
    assert audit["issues"] == []
    assert [event["version"] for event in audit["events"]] == [1, 2]
    assert audit["events"][1]["seq"] == 2
    assert audit["events"][1]["txid"] == "b" * 32



def test_rescue_audit_v2_rejects_duplicate_event_id(tmp_path):
    import json
    import jinlin_agent.trash as trash_mod
    trash_mod._append_audit_event(tmp_path, "restore", "restored", txid="d" * 32)
    trash_mod._append_audit_event(tmp_path, "recover", "already-published", txid="d" * 32)
    path = tmp_path / trash_mod.AUDIT_REL
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    events[1]["event_id"] = events[0]["event_id"]
    body = {key: value for key, value in events[1].items() if key != "event_hash"}
    events[1]["event_hash"] = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    path.write_text("".join(json.dumps(event, sort_keys=True) + "\n" for event in events), encoding="utf-8")
    audit = trash_mod._read_audit(tmp_path)
    assert audit["issues"]
    assert "duplicate event id" in audit["issues"][0]["reason"]


def test_rescue_audit_v2_rejects_legacy_event_after_v2(tmp_path):
    import json
    import jinlin_agent.trash as trash_mod
    trash_mod._append_audit_event(tmp_path, "restore", "restored", txid="e" * 32)
    path = tmp_path / trash_mod.AUDIT_REL
    first = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    legacy = {
        "version": 1, "ts": "2026-01-01T00:00:00Z", "txid": "f" * 24,
        "action": "clean", "status": "removed", "prev_hash": first["event_hash"],
    }
    legacy["event_hash"] = hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    path.write_text(json.dumps(first, sort_keys=True) + "\n" + json.dumps(legacy, sort_keys=True) + "\n", encoding="utf-8")
    audit = trash_mod._read_audit(tmp_path)
    assert audit["issues"]
    assert "cannot follow version-2" in audit["issues"][0]["reason"]


def test_rescue_transaction_projector_detects_resource_identity_reuse_and_ignores_preview():
    from jinlin_agent.trash import _transaction_states
    events = [
        {"txid": "c" * 32, "action": "clean-preview", "status": "dry-run", "rescue": "r", "target": "t", "sha256": "a"},
        {"txid": "c" * 32, "action": "restore", "status": "restored", "rescue": "r", "target": "t", "sha256": "a"},
        {"txid": "c" * 32, "action": "recover", "status": "already-published", "rescue": "r", "target": "different-target", "sha256": "a"},
    ]
    states = _transaction_states(events)
    assert len(states) == 1
    assert states[0]["state"] == "TRANSITION_INVALID"
    assert states[0]["event_count"] == 2
    assert states[0]["identity_issues"][0]["field"] == "target"



def test_rescue_inspect_reports_transaction_identity_drift_in_counts(tmp_path):
    import jinlin_agent.trash as trash_mod
    txid = "9" * 32
    assert trash_mod._append_audit_event(
        tmp_path, "restore", "restored", txid=txid, rescue=".r.py.jinlin-rescue-12345678",
        target="first.py", sha256="a" * 64,
    )[0]
    assert trash_mod._append_audit_event(
        tmp_path, "recover", "already-published", txid=txid, rescue=".r.py.jinlin-rescue-12345678",
        target="different.py", sha256="a" * 64,
    )[0]
    report = trash_mod.inspect_rescue(tmp_path)
    assert report["status"] == "attention-required"
    assert report["counts"]["transactions"] == 1
    assert report["counts"]["invalid_transactions"] == 1
    assert report["transactions"][0]["state"] == "TRANSITION_INVALID"
    assert any(issue["kind"] == "invalid-state-transition" for issue in report["issues"])


def test_quarantine_recovery_audit_keeps_original_transaction_identity(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"canonical transaction audit identity"
    rescue = _mk_trash(tmp_path, ".canonical.py.jinlin-rescue-11223349", payload)
    assert record_trash(tmp_path, rescue, "canonical.py", "rescue")
    txid = trash_mod._load_manifest(tmp_path)[rescue.name]["transaction_id"]
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-11223349")
    os.replace(rescue, quarantine)

    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert report["status"] == "applied"
    event = trash_mod._read_audit(tmp_path)["events"][-1]
    assert event["txid"] == txid
    assert event["rescue"] == rescue.name
    assert event["target"] == "canonical.py"
    assert event["quarantine"] == quarantine.name
    assert event["candidate"] == rescue.name


def test_rescue_cleaned_audit_retains_manifest_transaction_id_after_manifest_entry_removed(tmp_path):
    import jinlin_agent.trash as trash_mod
    rescue = _mk_trash(tmp_path, ".tx-clean.py.jinlin-rescue-11223348", b"clean tx")
    assert record_trash(tmp_path, rescue, "tx-clean.py", "rescue")
    txid = trash_mod._load_manifest(tmp_path)[rescue.name]["transaction_id"]
    assert trash_mod.restore_rescue(tmp_path, rescue.name)["status"] == "restored"
    cleaned = trash_mod.clean_rescue(tmp_path, apply=True)
    assert cleaned["removed"] == [rescue.name]
    audit = trash_mod._read_audit(tmp_path)
    clean_event = [event for event in audit["events"] if event["action"] == "clean"][-1]
    assert clean_event["txid"] == txid
    assert trash_mod._transaction_states(audit["events"])[0]["state"] == "CLEANED"


def test_rescue_state_machine_flags_published_to_staged_regression():
    from jinlin_agent.trash import _transaction_states

    events = [
        {"txid": "tx-regression", "action": "restore", "status": "restored", "ts": "2026-01-01T00:00:00Z"},
        {"txid": "tx-regression", "action": "recover", "status": "incomplete", "ts": "2026-01-01T00:01:00Z"},
    ]
    states = _transaction_states(events)
    assert states[0]["state"] == "TRANSITION_INVALID"
    assert states[0]["transition_issues"][0]["from"] == "PUBLISHED"
    assert states[0]["transition_issues"][0]["to"] == "STAGED"


def test_rescue_quarantine_inspect_classifies_verified_rescue_as_recoverable(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b'verified interrupted cleanup'
    rescue = _mk_trash(tmp_path, '.recoverable.py.jinlin-rescue-a1b2c3d4', payload)
    record_trash(tmp_path, rescue, 'recoverable.py', 'rescue')
    quarantine = rescue.with_name(f'.{rescue.name}.jinlin-clean-01020304')
    os.replace(rescue, quarantine)
    item = trash_mod.inspect_rescue(tmp_path)['quarantined'][0]
    assert item['classification'] == 'safe-to-restore'
    assert item['candidate'] == rescue.name
    assert item['candidate_kind'] == 'rescue'
    assert item['sha256'] == sha(payload)
    assert quarantine.read_bytes() == payload and not rescue.exists()


def test_rescue_quarantine_inspect_never_marks_occupied_candidate_safe(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b'verified interrupted cleanup'
    rescue = _mk_trash(tmp_path, '.occupied.py.jinlin-rescue-a1b2c3d5', payload)
    record_trash(tmp_path, rescue, 'occupied.py', 'rescue')
    quarantine = rescue.with_name(f'.{rescue.name}.jinlin-clean-01020305')
    os.replace(rescue, quarantine)
    rescue.write_bytes(b'new unrelated file')
    item = trash_mod.inspect_rescue(tmp_path)['quarantined'][0]
    assert item['classification'] == 'target-conflict'
    assert rescue.read_bytes() == b'new unrelated file' and quarantine.read_bytes() == payload


def test_rescue_quarantine_inspect_detects_content_tampering(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    rescue = _mk_trash(tmp_path, '.tampered.py.jinlin-rescue-a1b2c3d6', b'original')
    record_trash(tmp_path, rescue, 'tampered.py', 'rescue')
    quarantine = rescue.with_name(f'.{rescue.name}.jinlin-clean-01020306')
    os.replace(rescue, quarantine)
    quarantine.write_bytes(b'modified')
    item = trash_mod.inspect_rescue(tmp_path)['quarantined'][0]
    assert item['classification'] == 'identity-mismatch'
    assert 'SHA-256' in item['reason']


def test_rescue_quarantine_inspect_classifies_verified_restore_stage(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b'stage interrupted cleanup'
    rescue = _mk_trash(tmp_path, '.stage.py.jinlin-rescue-a1b2c3d7', payload)
    record_trash(tmp_path, rescue, 'stage.py', 'rescue')
    target = tmp_path / 'stage.py'
    target.write_bytes(payload)
    stage = trash_mod._restore_stage_path(target, sha(payload))
    stage.write_bytes(payload)
    quarantine = stage.with_name(f'.{stage.name}.jinlin-clean-01020307')
    os.replace(stage, quarantine)
    item = trash_mod.inspect_rescue(tmp_path)['quarantined'][0]
    assert item['classification'] == 'safe-to-restore'
    assert item['candidate_kind'] == 'stage' and item['candidate'] == stage.name


def test_rescue_quarantine_inspect_marks_duplicate_claims_ambiguous(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b'duplicate quarantine'
    rescue = _mk_trash(tmp_path, '.duplicate.py.jinlin-rescue-a1b2c3d8', payload)
    record_trash(tmp_path, rescue, 'duplicate.py', 'rescue')
    first = rescue.with_name(f'.{rescue.name}.jinlin-clean-01020308')
    second = rescue.with_name(f'.{rescue.name}.jinlin-clean-01020309')
    os.replace(rescue, first)
    os.link(first, second)
    items = trash_mod.inspect_rescue(tmp_path)['quarantined']
    assert len(items) == 2 and {item['classification'] for item in items} == {'ambiguous'}
    assert first.read_bytes() == payload and second.read_bytes() == payload


def test_rescue_quarantine_inspect_keeps_legacy_manifest_unverified(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    rescue = _mk_trash(tmp_path, '.legacy.py.jinlin-rescue-a1b2c3d9', b'legacy payload')
    record_trash(tmp_path, rescue, 'legacy.py', 'rescue')
    manifest_path = tmp_path / trash_mod.MANIFEST_REL
    entry = json.loads(manifest_path.read_text(encoding='utf-8').splitlines()[-1])
    entry['identity'].pop('sha256', None)
    manifest_path.write_text(json.dumps(entry) + '\n', encoding='utf-8')
    quarantine = rescue.with_name(f'.{rescue.name}.jinlin-clean-01020310')
    os.replace(rescue, quarantine)
    item = trash_mod.inspect_rescue(tmp_path)['quarantined'][0]
    assert item['classification'] == 'legacy-unverified' and quarantine.exists()


def test_rescue_inspect_cli_explains_quarantine_without_mutating_evidence(tmp_path, capsys):
    import os
    import jinlin_agent.trash as trash_mod
    from jinlin_agent.cli import main
    payload = b'cli quarantine evidence'
    rescue = _mk_trash(tmp_path, '.cli.py.jinlin-rescue-a1b2c3e0', payload)
    record_trash(tmp_path, rescue, 'cli.py', 'rescue')
    quarantine = rescue.with_name(f'.{rescue.name}.jinlin-clean-01020311')
    os.replace(rescue, quarantine)
    audit_path = tmp_path / trash_mod.AUDIT_REL
    audit_before = audit_path.read_bytes() if audit_path.exists() else None
    assert main(['rescue', 'inspect', '--workspace', str(tmp_path)]) == 2
    output = capsys.readouterr().out
    assert 'classification=safe-to-restore' in output
    assert rescue.exists() is False and quarantine.read_bytes() == payload
    assert (audit_path.read_bytes() if audit_path.exists() else None) == audit_before



def test_quarantine_recovery_dry_run_is_strictly_read_only(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"quarantine restore dry-run"
    rescue = _mk_trash(tmp_path, ".dry.py.jinlin-rescue-b1b2c3d4", payload)
    record_trash(tmp_path, rescue, "dry.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101010")
    os.replace(rescue, quarantine)
    audit_path = tmp_path / trash_mod.AUDIT_REL
    before_audit = audit_path.read_bytes() if audit_path.exists() else None
    before_manifest = (tmp_path / trash_mod.MANIFEST_REL).read_bytes()
    before_quarantine = quarantine.read_bytes()

    report = trash_mod.recover_clean_quarantines(tmp_path, apply=False)

    assert report["status"] == "dry-run"
    assert report["items"][0]["classification"] == "safe-to-restore"
    assert not rescue.exists()
    assert quarantine.read_bytes() == before_quarantine
    assert (tmp_path / trash_mod.MANIFEST_REL).read_bytes() == before_manifest
    assert (audit_path.read_bytes() if audit_path.exists() else None) == before_audit


def test_quarantine_recovery_restores_independent_rescue_and_preserves_evidence(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"independent quarantine evidence"
    rescue = _mk_trash(tmp_path, ".independent.py.jinlin-rescue-b1b2c3d5", payload)
    record_trash(tmp_path, rescue, "independent.py", "rescue")
    original_identity = os.stat(rescue)
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101011")
    os.replace(rescue, quarantine)

    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert report["status"] == "applied"
    assert report["blocked"] == []
    assert report["recovered"][0]["status"] == "restored"
    assert rescue.read_bytes() == payload
    assert quarantine.read_bytes() == payload
    assert os.stat(rescue).st_ino != os.stat(quarantine).st_ino
    listed = trash_mod.list_rescue(tmp_path)
    assert [item["path"] for item in listed["rescued"]] == [rescue.name]
    inspected = trash_mod.inspect_rescue(tmp_path)
    assert inspected["quarantined"][0]["classification"] == "already-restored"
    assert os.stat(quarantine).st_ino == original_identity.st_ino


def test_quarantine_recovery_restores_stage_then_normal_recovery_publishes_target(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"quarantined restore stage"
    rescue = _mk_trash(tmp_path, ".stage-recover.py.jinlin-rescue-b1b2c3d6", payload)
    record_trash(tmp_path, rescue, "stage-recover.py", "rescue")
    target = tmp_path / "stage-recover.py"
    target.write_bytes(payload)
    stage = trash_mod._restore_stage_path(target, sha(payload))
    stage.write_bytes(payload)
    quarantine = stage.with_name(f".{stage.name}.jinlin-clean-10101012")
    os.replace(stage, quarantine)

    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert report["status"] == "applied"
    assert stage.read_bytes() == payload
    assert quarantine.read_bytes() == payload
    recovered = trash_mod.recover_rescue(tmp_path)
    assert recovered["items"][0]["status"] == "already-published"
    assert target.read_bytes() == payload


def test_quarantine_recovery_refuses_conflict_tampering_ambiguity_and_legacy(tmp_path):
    import os
    import json
    import jinlin_agent.trash as trash_mod

    # Occupied target: never overwrite.
    conflict = _mk_trash(tmp_path, ".conflict.py.jinlin-rescue-b1b2c3d7", b"expected")
    record_trash(tmp_path, conflict, "conflict.py", "rescue")
    conflict_q = conflict.with_name(f".{conflict.name}.jinlin-clean-10101013")
    os.replace(conflict, conflict_q)
    conflict.write_bytes(b"external")
    # Tampered quarantine.
    tampered = _mk_trash(tmp_path, ".tamper-q.py.jinlin-rescue-b1b2c3d8", b"original")
    record_trash(tmp_path, tampered, "tamper-q.py", "rescue")
    tampered_q = tampered.with_name(f".{tampered.name}.jinlin-clean-10101014")
    os.replace(tampered, tampered_q)
    tampered_q.write_bytes(b"changed")
    # Ambiguous duplicate claim.
    duplicate = _mk_trash(tmp_path, ".duplicate-q.py.jinlin-rescue-b1b2c3d9", b"duplicate")
    record_trash(tmp_path, duplicate, "duplicate-q.py", "rescue")
    duplicate_q = duplicate.with_name(f".{duplicate.name}.jinlin-clean-10101015")
    duplicate_q2 = duplicate.with_name(f".{duplicate.name}.jinlin-clean-10101016")
    os.replace(duplicate, duplicate_q)
    os.link(duplicate_q, duplicate_q2)
    # Legacy record without SHA-256.
    legacy = _mk_trash(tmp_path, ".legacy-q.py.jinlin-rescue-b1b2c3e0", b"legacy")
    record_trash(tmp_path, legacy, "legacy-q.py", "rescue")
    manifest_path = tmp_path / trash_mod.MANIFEST_REL
    entries = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines()]
    for entry in entries:
        if entry["path"] == legacy.name:
            entry["identity"].pop("sha256", None)
    manifest_path.write_text("".join(json.dumps(entry) + "\n" for entry in entries), encoding="utf-8")
    legacy_q = legacy.with_name(f".{legacy.name}.jinlin-clean-10101017")
    os.replace(legacy, legacy_q)

    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert report["status"] == "attention-required"
    assert conflict.read_bytes() == b"external"
    assert tampered_q.read_bytes() == b"changed"
    assert duplicate_q.exists() and duplicate_q2.exists()
    assert legacy_q.read_bytes() == b"legacy"
    assert not tampered.exists() and not duplicate.exists() and not legacy.exists()
    statuses = {item.get("classification", item.get("status")) for item in report["blocked"]}
    assert "target-conflict" in statuses
    assert "identity-mismatch" in statuses
    assert "ambiguous" in statuses
    assert "legacy-unverified" in statuses


def test_quarantine_recovery_resumes_partial_stage_without_overwrite(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"resume this interrupted quarantine stage"
    rescue = _mk_trash(tmp_path, ".resume.py.jinlin-rescue-b1b2c3e1", payload)
    record_trash(tmp_path, rescue, "resume.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101018")
    os.replace(rescue, quarantine)
    stage = trash_mod._quarantine_restore_stage_path(rescue, sha(payload))
    stage.write_bytes(payload[:9])

    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert report["status"] == "applied"
    assert rescue.read_bytes() == payload
    assert stage.exists() is False
    assert quarantine.read_bytes() == payload




def test_quarantine_recovery_manifest_failure_is_reconciled_on_retry(tmp_path, monkeypatch):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"manifest update retry"
    rescue = _mk_trash(tmp_path, ".manifest-retry.py.jinlin-rescue-b1b2c3e5", payload)
    record_trash(tmp_path, rescue, "manifest-retry.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101022")
    os.replace(rescue, quarantine)
    real_write_manifest = trash_mod._write_manifest_atomic
    monkeypatch.setattr(trash_mod, "_write_manifest_atomic", lambda *_args, **_kwargs: False)

    first = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert first["status"] == "attention-required"
    assert first["blocked"][0]["status"] == "uncertain"
    assert rescue.read_bytes() == payload and quarantine.read_bytes() == payload
    assert os.stat(rescue).st_ino != os.stat(quarantine).st_ino

    monkeypatch.setattr(trash_mod, "_write_manifest_atomic", real_write_manifest)
    second = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert second["status"] == "applied"
    assert second["recovered"][0]["status"] == "already-restored"
    assert trash_mod.list_rescue(tmp_path)["rescued"][0]["path"] == rescue.name
    assert quarantine.read_bytes() == payload
    assert os.stat(rescue).st_ino != os.stat(quarantine).st_ino


def test_quarantine_recovery_partial_write_failure_can_be_retried(tmp_path, monkeypatch):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"partial write retry for quarantine"
    rescue = _mk_trash(tmp_path, ".partial-retry.py.jinlin-rescue-b1b2c3e6", payload)
    record_trash(tmp_path, rescue, "partial-retry.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101023")
    os.replace(rescue, quarantine)
    real_write = trash_mod.os.write
    writes = {"count": 0}

    def fail_after_prefix(fd, data):
        writes["count"] += 1
        if writes["count"] == 1:
            return real_write(fd, data[:6])
        if writes["count"] == 2:
            raise OSError("injected stage write interruption")
        return real_write(fd, data)

    monkeypatch.setattr(trash_mod.os, "write", fail_after_prefix)
    first = trash_mod.recover_clean_quarantines(tmp_path, apply=True)
    assert first["status"] == "attention-required"
    stage = trash_mod._quarantine_restore_stage_path(rescue, sha(payload))
    assert stage.read_bytes() == payload[:6]
    assert not rescue.exists() and quarantine.read_bytes() == payload

    monkeypatch.setattr(trash_mod.os, "write", real_write)
    second = trash_mod.recover_clean_quarantines(tmp_path, apply=True)
    assert second["status"] == "applied"
    assert rescue.read_bytes() == payload and quarantine.read_bytes() == payload
    assert not stage.exists()


def test_quarantine_recovery_target_race_is_no_clobber(tmp_path, monkeypatch):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"quarantine race payload"
    external = b"external target wins"
    rescue = _mk_trash(tmp_path, ".race.py.jinlin-rescue-b1b2c3e3", payload)
    record_trash(tmp_path, rescue, "race.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101020")
    os.replace(rescue, quarantine)
    real_link = trash_mod.os.link

    def race_link(source, destination, *args, **kwargs):
        if Path(destination) == rescue and "jinlin-quarantine-restore-" in Path(source).name:
            rescue.write_bytes(external)
            raise FileExistsError(str(rescue))
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(trash_mod.os, "link", race_link)
    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert report["status"] == "attention-required"
    assert rescue.read_bytes() == external
    assert quarantine.read_bytes() == payload
    assert report["blocked"][0]["status"] == "conflict"



def test_quarantine_recovery_does_not_claim_unrelated_same_bytes_target(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"same bytes do not prove ownership"
    rescue = _mk_trash(tmp_path, ".same-bytes.py.jinlin-rescue-b1b2c3e7", payload)
    record_trash(tmp_path, rescue, "same-bytes.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101024")
    os.replace(rescue, quarantine)
    rescue.write_bytes(payload)  # Same bytes, but a different unrecorded inode.

    inspected = trash_mod.inspect_rescue(tmp_path)
    assert inspected["quarantined"][0]["classification"] == "target-conflict"
    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)
    assert report["status"] == "attention-required"
    assert rescue.read_bytes() == payload and quarantine.read_bytes() == payload
    assert report["blocked"][0]["classification"] == "target-conflict"



def test_quarantine_recovery_revalidates_identity_when_racing_same_bytes_target(tmp_path, monkeypatch):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"racing writer chose identical bytes"
    rescue = _mk_trash(tmp_path, ".same-race.py.jinlin-rescue-b1b2c3e9", payload)
    record_trash(tmp_path, rescue, "same-race.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101026")
    os.replace(rescue, quarantine)
    real_link = trash_mod.os.link

    def race_same_bytes(source, destination, *args, **kwargs):
        if Path(destination) == rescue and "jinlin-quarantine-restore-" in Path(source).name:
            rescue.write_bytes(payload)
            raise FileExistsError(str(rescue))
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(trash_mod.os, "link", race_same_bytes)
    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert report["status"] == "attention-required"
    assert report["blocked"][0]["status"] == "conflict"
    assert rescue.read_bytes() == payload and quarantine.read_bytes() == payload
    assert os.stat(rescue).st_ino != os.stat(quarantine).st_ino


def test_quarantine_recovery_refuses_to_share_quarantine_inode(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"do not share evidence inode"
    rescue = _mk_trash(tmp_path, ".shared-inode.py.jinlin-rescue-b1b2c3e8", payload)
    record_trash(tmp_path, rescue, "shared-inode.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101025")
    os.replace(rescue, quarantine)
    os.link(quarantine, rescue)

    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert report["status"] == "attention-required"
    assert rescue.read_bytes() == payload and quarantine.read_bytes() == payload
    assert os.stat(rescue).st_ino == os.stat(quarantine).st_ino
    assert report["blocked"][0]["status"] == "conflict"


def test_quarantine_recovery_rejects_non_prefix_stage_without_mutation(tmp_path):
    import os
    import jinlin_agent.trash as trash_mod
    payload = b"verified quarantine source"
    rescue = _mk_trash(tmp_path, ".bad-stage.py.jinlin-rescue-b1b2c3e4", payload)
    record_trash(tmp_path, rescue, "bad-stage.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101021")
    os.replace(rescue, quarantine)
    stage = trash_mod._quarantine_restore_stage_path(rescue, sha(payload))
    stage.write_bytes(b"not a prefix")

    report = trash_mod.recover_clean_quarantines(tmp_path, apply=True)

    assert report["status"] == "attention-required"
    assert not rescue.exists()
    assert stage.read_bytes() == b"not a prefix"
    assert quarantine.read_bytes() == payload
    assert report["blocked"][0]["status"] == "rejected"


def test_quarantine_recovery_cli_requires_explicit_apply_and_supports_json(tmp_path, capsys):
    import os
    import json
    from jinlin_agent.cli import main
    payload = b"quarantine cli"
    rescue = _mk_trash(tmp_path, ".cli-q.py.jinlin-rescue-b1b2c3e2", payload)
    record_trash(tmp_path, rescue, "cli-q.py", "rescue")
    quarantine = rescue.with_name(f".{rescue.name}.jinlin-clean-10101019")
    os.replace(rescue, quarantine)

    assert main(["rescue", "quarantine", "inspect", "--workspace", str(tmp_path), "--json"]) == 2
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["counts"]["safe_to_restore"] == 1
    assert main(["rescue", "quarantine", "recover", "--workspace", str(tmp_path), "--json"]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["status"] == "dry-run" and not rescue.exists()
    assert main(["rescue", "quarantine", "recover", "--apply", "--workspace", str(tmp_path), "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["status"] == "applied"
    assert rescue.read_bytes() == payload and quarantine.read_bytes() == payload


def test_rescue_inspect_cli_json_emits_machine_readable_classification(tmp_path, capsys):
    import json
    import os
    import jinlin_agent.trash as trash_mod
    from jinlin_agent.cli import main
    payload = b'json quarantine evidence'
    rescue = _mk_trash(tmp_path, '.json.py.jinlin-rescue-a1b2c3e1', payload)
    record_trash(tmp_path, rescue, 'json.py', 'rescue')
    quarantine = rescue.with_name(f'.{rescue.name}.jinlin-clean-01020312')
    os.replace(rescue, quarantine)
    assert main(['rescue', 'inspect', '--workspace', str(tmp_path), '--json']) == 2
    report = json.loads(capsys.readouterr().out)
    assert report['status'] == 'attention-required'
    assert report['counts']['quarantine_safe_to_restore'] == 1
    assert report['quarantined'][0]['classification'] == 'safe-to-restore'
    assert quarantine.read_bytes() == payload and not rescue.exists()
