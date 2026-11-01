"""Regression tests for the independent review findings (P0/P1/P2).

These are the security/completion-gate contracts that must not regress:
workspace boundary through parent links, shell cwd, read-before-write on
write_file, atomic-write hash guard, L0/L5/tests evidence, event-log integrity.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jinlin_agent.config import load_config
from jinlin_agent.domain import PlanStep, TaskPlan
from jinlin_agent.errors import AgentError
from jinlin_agent.events import EventLog, read_events
from jinlin_agent.paths import resolve_in_workspace
from jinlin_agent.policy import PolicyEngine
from jinlin_agent.sandbox.base import RunResult
from jinlin_agent.sandbox.null import NullSandbox
from jinlin_agent.state import SessionState
from jinlin_agent.tools.base import ToolContext
from jinlin_agent.tools.patch import atomic_write, write_file
from jinlin_agent.verify import CompletionGate, VerificationRun, Verifier, parse_test_output


def sha(data: bytes) -> str:
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


# --------------------------------------------------------------------------- P0 paths


def _make_junction(link: Path, target: Path) -> bool:
    if sys.platform != "win32":
        return False
    target.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.returncode == 0 and link.exists()


def test_parent_junction_escape_rejected(tmp_path):
    ws = tmp_path / "ws"
    outside = tmp_path / "outside"
    ws.mkdir()
    link = ws / "external-link"
    if not _make_junction(link, outside):
        pytest.skip("cannot create junction in this environment")
    # final target does not exist yet; the parent link escapes the workspace
    with pytest.raises(AgentError) as excinfo:
        resolve_in_workspace(ws, "external-link/new_file.py")
    assert excinfo.value.code == "PATH_OUTSIDE_WORKSPACE"
    with pytest.raises(AgentError):
        make_ctx(ws).resolve("external-link/new_file.py")


def test_dotdot_and_absolute_escape_rejected(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    for raw in ("../outside.txt", str(tmp_path / "outside.txt")):
        with pytest.raises(AgentError) as excinfo:
            resolve_in_workspace(ws, raw)
        assert excinfo.value.code == "PATH_OUTSIDE_WORKSPACE"
    # legitimate nested path still works
    assert resolve_in_workspace(ws, "pkg/mod.py") == ws / "pkg" / "mod.py"


def test_shell_cwd_outside_denied(tmp_path):
    cfg = load_config(tmp_path, home=tmp_path / "home")
    engine = PolicyEngine(cfg, tmp_path)
    decision = engine.evaluate("run_command", "EXECUTE", {"command": ["git", "status"], "cwd": ".."})
    assert not decision.allowed
    assert decision.error is not None and decision.error.code == "PATH_OUTSIDE_WORKSPACE"


# --------------------------------------------------------------------------- P1 edit


def test_write_file_existing_requires_read(tmp_path):
    ctx = make_ctx(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    result = write_file(ctx, {"path": "a.py", "content": "x = 2\n"})
    assert not result.ok
    assert result.error is not None and result.error.code == "FILE_NOT_READ"
    assert target.read_text(encoding="utf-8") == "x = 1\n"


def test_write_file_external_change_rejected(tmp_path):
    ctx = make_ctx(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("x = 1\n", encoding="utf-8")
    ctx.state.read_hashes["a.py"] = sha(target.read_bytes())
    target.write_text("x = 99\n", encoding="utf-8")
    result = write_file(ctx, {"path": "a.py", "content": "x = 2\n"})
    assert not result.ok
    assert result.error is not None and result.error.code == "FILE_CHANGED_EXTERNALLY"
    assert "99" in target.read_text(encoding="utf-8")


def test_atomic_write_hash_guard(tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("old", encoding="utf-8")
    with pytest.raises(AgentError) as excinfo:
        atomic_write(target, b"new", expected_sha256=sha(b"not-what-is-there"))
    assert excinfo.value.code == "FILE_CHANGED_EXTERNALLY"
    assert target.read_text(encoding="utf-8") == "old"


# --------------------------------------------------------------------------- P2 events


def _write_events(path: Path, lines: list[str]) -> None:
    """Write newline-terminated JSONL (normal case: every line is complete)."""
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_bytes(path: Path, data: bytes) -> None:
    path.write_bytes(data)


def _evt(seq: int) -> str:
    return json.dumps({"schema_version": 1, "seq": seq, "type": "session.created", "payload": {}})


def test_mid_log_corruption_is_reported(tmp_path):
    _write_events(tmp_path / "events.jsonl", [_evt(1), "{broken", _evt(3)])
    with pytest.raises(AgentError) as excinfo:
        list(read_events(tmp_path))
    assert excinfo.value.code == "SESSION_CORRUPTED"


def test_seq_gap_is_reported(tmp_path):
    _write_events(tmp_path / "events.jsonl", [_evt(1), _evt(3)])
    with pytest.raises(AgentError) as excinfo:
        list(read_events(tmp_path))
    assert excinfo.value.code == "SESSION_CORRUPTED"


def test_torn_tail_without_newline_is_tolerated(tmp_path):
    # genuine crash artifact: incomplete trailing bytes with no newline
    _write_bytes(tmp_path / "events.jsonl", (_evt(1) + "\n").encode() + b'{"seq":')
    events = list(read_events(tmp_path))
    assert [e["seq"] for e in events] == [1]


def test_newline_terminated_corrupt_tail_is_rejected(tmp_path):
    _write_bytes(tmp_path / "events.jsonl", (_evt(1) + "\n").encode() + b"{broken}\n")
    with pytest.raises(AgentError) as excinfo:
        list(read_events(tmp_path))
    assert excinfo.value.code == "SESSION_CORRUPTED"


def test_blank_line_between_events_is_rejected(tmp_path):
    _write_bytes(tmp_path / "events.jsonl", (_evt(1) + "\n\n" + _evt(2) + "\n").encode())
    with pytest.raises(AgentError) as excinfo:
        list(read_events(tmp_path))
    assert excinfo.value.code == "SESSION_CORRUPTED"


def test_schema_and_session_mismatch_is_rejected(tmp_path):
    bad_schema = json.dumps({"schema_version": 99, "seq": 2, "type": "x", "payload": {}})
    _write_events(tmp_path / "events.jsonl", [_evt(1), bad_schema])
    with pytest.raises(AgentError):
        list(read_events(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    first = json.dumps({"schema_version": 1, "seq": 1, "type": "x", "payload": {}, "session_id": "a"})
    second = json.dumps({"schema_version": 1, "seq": 2, "type": "x", "payload": {}, "session_id": "b"})
    _write_events(other / "events.jsonl", [first, second])
    with pytest.raises(AgentError) as excinfo:
        list(read_events(other))
    assert excinfo.value.code == "SESSION_CORRUPTED"


# ------------------------------------------------------------------- P1 torn-tail repair


def test_open_repairs_torn_tail_before_append(tmp_path):
    """A tolerated torn tail must be truncated before the next append; appending
    behind an unterminated fragment would fuse both into one corrupt line and
    permanently corrupt the log on the next read."""
    log_path = tmp_path / "events.jsonl"
    _write_bytes(log_path, (_evt(1) + "\n").encode() + b'{"seq":')
    log = EventLog(tmp_path, "sess_torn").open()
    try:
        log.append("step.started")
    finally:
        log.close()
    events = list(read_events(tmp_path))
    assert [e["seq"] for e in events] == [1, 2]
    data = log_path.read_bytes()
    assert data.endswith(b"\n"), "append must never follow an unterminated fragment"
    for line in data.split(b"\n")[:-1]:
        json.loads(line.decode("utf-8"))  # every remaining line must parse


def test_open_repairs_missing_trailing_newline(tmp_path):
    """A complete final JSON line whose newline was lost must get its terminator
    back before the next append, so two events never share one line."""
    _write_bytes(tmp_path / "events.jsonl", _evt(1).encode())  # valid JSON, no \n
    log = EventLog(tmp_path, "sess_nonl").open()
    try:
        log.append("step.started")
    finally:
        log.close()
    events = list(read_events(tmp_path))
    assert [e["seq"] for e in events] == [1, 2]
    data = (tmp_path / "events.jsonl").read_bytes()
    assert len([line for line in data.split(b"\n") if line.strip()]) == 2


def test_open_failure_releases_session_lock(tmp_path):
    """A log that fails validation in open() must not leak the advisory lock;
    the next EventLog instance must be able to take it immediately."""
    _write_bytes(tmp_path / "events.jsonl", (_evt(1) + "\n").encode() + b"{broken}\n")
    with pytest.raises(AgentError) as excinfo:
        EventLog(tmp_path, "sess_lock").open()
    assert excinfo.value.code == "SESSION_CORRUPTED"
    second = EventLog(tmp_path, "sess_lock")
    second._lock.acquire()  # must not raise SessionLockedError
    second._lock.release()


# --------------------------------------------------------------------------- P1 gate


class FakeSandbox:
    engine = "fake"
    level = "L0"

    def __init__(self, outputs: list[RunResult] | None = None) -> None:
        self.outputs = list(outputs or [])
        self.calls: list[list[str]] = []

    def run(self, argv, cwd, timeout, env=None, stdin_data=None, max_output_chars=200_000):
        self.calls.append(list(argv))
        if self.outputs:
            return self.outputs.pop(0)
        return RunResult(exit_code=0, stdout="", stderr="", engine="fake")


class FakeArtifacts:
    def write(self, kind, content, meta=None):
        class A:
            artifact_id = "artifact_fake"

        return A()


def _gate_state(tmp_path: Path) -> SessionState:
    state = SessionState(session_id="s", workspace=str(tmp_path), request="fix it")
    state.plan = TaskPlan(
        objective="fix it",
        steps=[PlanStep(step_id="s1", description="fix", status="completed", evidence=["a.py"])],
    )
    state.written_files["a.py"] = {"hash_before": None, "hash_after": "deadbeef"}
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    return state


def _claim(statement: str, refs: list[str]):
    from jinlin_agent.domain import Claim

    return Claim(claim_id="c1", statement=statement, evidence_refs=refs)


def test_gate_rejects_failed_l0(tmp_path):
    cfg = load_config(tmp_path, home=tmp_path / "home")
    state = _gate_state(tmp_path)
    state.verification_runs = [
        VerificationRun(verification_id="v0", level="L0", type="diff-review", status="failed", findings=["suspicious sensitive content: a.py"]),
        VerificationRun(verification_id="v1", level="L1", type="structure", status="passed"),
    ]
    gate = CompletionGate(tmp_path, cfg, state, FakeSandbox(), verifier=None)
    result = gate.check([_claim("fixed the bug", ["a.py"])])
    assert not result.passed
    assert any("L0" in r for r in result.reasons)


def test_gate_requires_passing_test_evidence_for_test_claims(tmp_path):
    cfg = load_config(tmp_path, home=tmp_path / "home")
    state = _gate_state(tmp_path)
    state.verification_runs = [
        VerificationRun(verification_id="v0", level="L0", type="diff-review", status="passed"),
        VerificationRun(verification_id="v1", level="L1", type="structure", status="passed"),
        VerificationRun(
            verification_id="v3",
            level="L3",
            type="tests",
            status="passed",
            command=["python", "-m", "pytest", "-q"],
        ),
    ]
    verifier = Verifier(tmp_path, cfg, FakeSandbox(), FakeArtifacts(), emit=lambda t, p: {}, state=state)
    verifier.project.test_commands = [["python", "-m", "pytest", "-q"]]
    gate = CompletionGate(tmp_path, cfg, state, FakeSandbox(), verifier=verifier)
    # README-style file evidence cannot back a "tests pass" claim
    result = gate.check([_claim("all tests pass", ["a.py"])])
    assert not result.passed
    assert any("test verification" in r for r in result.reasons)
    # citing the passing L3 verification is accepted
    result2 = gate.check([_claim("all tests pass", ["L3 tests", "a.py"])])
    assert result2.passed, result2.reasons


# --------------------------------------------------------------------------- P1 L5


def test_parse_test_output_extracts_failure_ids():
    text = "FAILED tests/test_a.py::test_one - assert 1 == 2\nERROR tests/test_b.py::test_two\n1 failed, 1 error"
    parsed = parse_test_output(text, exit_code=1)
    assert parsed["failed_ids"] == ["tests/test_a.py::test_one"]
    assert parsed["error_ids"] == ["tests/test_b.py::test_two"]
    assert parsed["failed"] == 1 and parsed["errors"] == 1


def test_l5_detects_changed_failure_identity(tmp_path):
    cfg = load_config(tmp_path, home=tmp_path / "home")
    state = _gate_state(tmp_path)
    state.baseline = {
        "command": ["pytest", "-q"],
        "status": "failed",
        "exit_code": 1,
        "failed_ids": ["tests/test_a.py::test_a"],
        "error_ids": [],
    }
    sandbox = FakeSandbox(
        [RunResult(exit_code=1, stdout="FAILED tests/test_b.py::test_b\n1 failed", engine="fake")]
    )
    verifier = Verifier(tmp_path, cfg, sandbox, FakeArtifacts(), emit=lambda t, p: {}, state=state)
    run = verifier._level5()
    assert run.status == "failed"
    assert any("test_b" in f for f in run.findings)


def test_l5_none_exit_code_fails_closed(tmp_path):
    """Unknown process state (no exit code) must not be treated as a passing regression check."""
    cfg = load_config(tmp_path, home=tmp_path / "home")
    state = _gate_state(tmp_path)
    state.baseline = {
        "command": ["pytest", "-q"],
        "status": "passed",
        "exit_code": 0,
        "failed_ids": [],
        "error_ids": [],
    }
    sandbox = FakeSandbox([RunResult(exit_code=None, timed_out=False, stdout="", stderr="", engine="fake")])
    verifier = Verifier(tmp_path, cfg, sandbox, FakeArtifacts(), emit=lambda t, p: {}, state=state)
    run = verifier._level5()
    assert run.status == "failed"
    assert any("no exit code" in f for f in run.findings)


def test_edit_invalidates_stale_bytecode(tmp_path):
    """A same-second edit must not leave Python re-using the previous .pyc."""
    target = tmp_path / "calc.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    subprocess.run([sys.executable, "-c", "import calc"], cwd=str(tmp_path), check=True)
    before = list((tmp_path / "__pycache__").glob("calc.*.pyc"))
    assert before, "expected an import to produce bytecode"
    old_sha = sha(target.read_bytes())
    atomic_write(
        target,
        b"def add(a, b):\n    return a + b\n",
        mode_from=target,
        expected_sha256=old_sha,
    )
    remaining = list((tmp_path / "__pycache__").glob("calc.*.pyc"))
    assert not remaining, "editing a .py file must invalidate its cached bytecode"


# --------------------------------------------------------------------------- P1 patch rollback


def _patch_text(*ops: str) -> str:
    return "*** Begin Patch\n" + "\n".join(ops) + "\n*** End Patch"


def test_apply_patch_add_conflict_does_not_delete_external_file(tmp_path, monkeypatch):
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    external = tmp_path / "new.py"
    real_atomic = patch_mod.atomic_write

    def racing(path, data, mode_from=None, expected_sha256=None, create=False):
        if create and not path.exists():
            path.write_bytes(b"external-writer")  # another process wins the race
        return real_atomic(path, data, mode_from=mode_from, expected_sha256=expected_sha256, create=create)

    monkeypatch.setattr(patch_mod, "atomic_write", racing)
    patch = _patch_text("*** Add File: new.py", "+created by agent")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert external.read_bytes() == b"external-writer", "rollback must never delete a concurrent writer's file"


def test_apply_patch_update_conflict_does_not_restore_over_external_edit(tmp_path, monkeypatch):
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["a.py"] = sha(target.read_bytes())
    real_atomic = patch_mod.atomic_write

    def racing(path, data, mode_from=None, expected_sha256=None, create=False):
        if expected_sha256 is not None:
            path.write_bytes(b"external-edit")  # concurrent modification after validation
        return real_atomic(path, data, mode_from=mode_from, expected_sha256=expected_sha256, create=create)

    monkeypatch.setattr(patch_mod, "atomic_write", racing)
    patch = _patch_text("*** Update File: a.py", "@@", "-old", "+new")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert target.read_bytes() == b"external-edit", "rollback must not overwrite concurrent content"


def test_apply_patch_rollback_only_reverts_own_mutations(tmp_path, monkeypatch):
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
            result = real_atomic(path, data, mode_from=mode_from, expected_sha256=expected_sha256, create=create)
            return result
        # before failing the second write, a concurrent writer changes the first file
        first.write_bytes(b"external-after-our-write")
        raise patch_mod.AgentError("PATCH_CONFLICT", "simulated concurrent conflict", source="edit")

    monkeypatch.setattr(patch_mod, "atomic_write", failing_second)
    patch = _patch_text(
        "*** Update File: a.py", "@@", "-old-a", "+new-a",
        "*** Update File: b.py", "@@", "-old-b", "+new-b",
    )
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "RECOVERY_UNCERTAIN"
    assert "a.py" in result.error.details.get("rollback_incomplete", [])
    assert first.read_bytes() == b"external-after-our-write", "incomplete rollback must keep external content"


def test_apply_patch_delete_conflict_preserves_external_file(tmp_path, monkeypatch):
    import jinlin_agent.tools.patch as patch_mod
    import os as os_mod
    from pathlib import Path as PathMod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_replace = os_mod.replace
    original_bytes = target.read_bytes()
    raced = {"done": False}

    def racing_replace(src, dst):
        if not raced["done"] and PathMod(src) == target:
            # external writer changes the file between validation and the directory operation
            target.write_bytes(b"external-doomed")
            raced["done"] = True
        return real_replace(src, dst)

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert target.exists(), "the file must be restored, not lost"
    # round-9 refined contract: the original bytes are re-created at the path
    # (they are provably ours via the fd hash); the external write that landed
    # in the moved backup is preserved aside — neither version is destroyed
    assert target.read_bytes() == b"external-doomed" or target.read_bytes() == original_bytes
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert backups, "the competing content is kept aside, never destroyed"


# ------------------------------------------------------------------ P1 TOCTOU windows (round 3)


def test_apply_patch_add_no_clobber_at_replace_window(tmp_path, monkeypatch):
    """The Add path must establish the file without os.replace at all: an
    exclusive create leaves no check-then-replace window in which a concurrent
    creator could be overwritten."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "new.py"
    real_replace = os.replace
    calls = {"replace": 0}

    def racing_replace(src, dst):
        calls["replace"] += 1
        if Path(dst) == target and not target.exists():
            target.write_bytes(b"external-writer")  # concurrent creator wins the race
        return real_replace(src, dst)

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    patch = _patch_text("*** Add File: new.py", "+created by agent")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert calls["replace"] == 0, "the create path must never route through os.replace"
    assert result.ok, "the race window no longer exists, so the Add completes cleanly"
    assert target.read_bytes() == b"created by agent\n"


def test_apply_patch_add_conflict_when_creator_wins_exclusive_create(tmp_path, monkeypatch):
    """A creator that slips in just before the exclusive create must win:
    the Add fails with PATCH_CONFLICT and the foreign content survives."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "new.py"
    real_open = os.open

    def racing_open(path, flags, *args, **kwargs):
        if isinstance(path, (str, bytes, os.PathLike)) and Path(path) == target and not target.exists():
            target.write_bytes(b"external-writer")  # concurrent creator inserts first
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(patch_mod.os, "open", racing_open)
    patch = _patch_text("*** Add File: new.py", "+created by agent")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "PATCH_CONFLICT"
    assert target.read_bytes() == b"external-writer"


def test_apply_patch_update_toctou_after_hash_check(tmp_path, monkeypatch):
    """An uncoordinated external write landing between the final hash check and
    os.replace cannot be detected with portable file APIs. The documented
    best-effort contract: jinlin sessions are mutually excluded by the workspace
    write lock (see the lock test), and the residual window against arbitrary
    external processes is stated in the security model instead of claimed
    impossible."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["a.py"] = sha(target.read_bytes())
    real_replace = os.replace

    def racing_replace(src, dst):
        if Path(dst) == target:
            target.write_bytes(b"external-edit-after-check")  # lands in the residual window
        return real_replace(src, dst)

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    patch = _patch_text("*** Update File: a.py", "@@", "-old", "+new")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert result.ok
    assert target.read_bytes().replace(b"\r\n", b"\n") == b"new\n"


def test_editing_tools_serialize_on_workspace_write_lock(tmp_path):
    """The workspace write lock is what turns the pre-write hash check into a
    hard guarantee between jinlin sessions: a second write phase cannot even
    start while another session holds the lock."""
    import jinlin_agent.tools.patch as patch_mod

    from jinlin_agent.events import SessionLock

    ctx = make_ctx(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["a.py"] = sha(target.read_bytes())
    blocker = SessionLock(tmp_path / ".jinlin" / "write.lock")
    blocker.acquire()
    try:
        patch = _patch_text("*** Update File: a.py", "@@", "-old", "+new")
        result = patch_mod.apply_patch(ctx, {"patch": patch})
        assert not result.ok
        assert result.error is not None and result.error.code == "WORKSPACE_LOCKED"
        assert target.read_text(encoding="utf-8") == "old\n", "the blocked write must not touch the file"
    finally:
        blocker.release()
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert result.ok
    assert target.read_text(encoding="utf-8") == "new\n"


def test_rollback_after_check_preserves_external_content(tmp_path, monkeypatch):
    """Rollback's own check-then-act: content written between the ownership
    check and the destructive step must never be deleted or overwritten. The
    verified content is moved aside atomically first, so a foreign edit rides
    along, fails the ownership hash, and is moved back untouched."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    first = tmp_path / "a.py"
    second = tmp_path / "b.py"
    first.write_text("old-a\n", encoding="utf-8")
    second.write_text("old-b\n", encoding="utf-8")
    ctx.state.read_hashes["a.py"] = sha(first.read_bytes())
    ctx.state.read_hashes["b.py"] = sha(second.read_bytes())
    real_atomic = patch_mod.atomic_write
    real_replace = os.replace
    calls = {"n": 0}

    def failing_second(path, data, mode_from=None, expected_sha256=None, create=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_atomic(path, data, mode_from=mode_from, expected_sha256=expected_sha256, create=create)
        raise patch_mod.AgentError("PATCH_CONFLICT", "simulated concurrent conflict", source="edit")

    def racing_replace(src, dst):
        dst_path = Path(dst)
        if dst_path == second:
            second.write_bytes(b"external-edit-in-window")  # old design: clobbered right here
        elif dst_path.name.startswith(".a.py.jinlin-rollback"):
            first.write_bytes(b"external-edit-in-window")  # new design: rides along in the trash
        return real_replace(src, dst)

    monkeypatch.setattr(patch_mod, "atomic_write", failing_second)
    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    patch = _patch_text(
        "*** Update File: a.py", "@@", "-old-a", "+new-a",
        "*** Update File: b.py", "@@", "-old-b", "+new-b",
    )
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "RECOVERY_UNCERTAIN"
    assert "a.py" in result.error.details.get("rollback_incomplete", [])
    assert first.read_bytes() == b"external-edit-in-window", "external content must survive the rollback"


# ------------------------------------------------------------------ P1 no-clobber restore (round 4)


@pytest.mark.parametrize("kind", ["add", "update"])
def test_rollback_restore_is_no_clobber_when_path_recreated(tmp_path, monkeypatch, kind):
    """Round-4 P1-1: while the rolled-back content sits in the transaction
    trash, an external writer can recreate the original path. The restore must
    be create-if-absent — keep both sides (external content at the path, our
    content in the trash) and report RECOVERY_UNCERTAIN; never replace."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "a.py"
    other = tmp_path / "b.py"
    if kind == "update":
        target.write_text("old\n", encoding="utf-8")
        ctx.state.read_hashes["a.py"] = sha(target.read_bytes())
        other.write_text("old-b\n", encoding="utf-8")
        ctx.state.read_hashes["b.py"] = sha(other.read_bytes())

    real_atomic = patch_mod.atomic_write
    real_link = os.link
    calls = {"n": 0}

    def failing_second(path, data, mode_from=None, expected_sha256=None, create=False):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_atomic(path, data, mode_from=mode_from, expected_sha256=expected_sha256, create=create)
        target.write_bytes(b"external-in-window")  # external edit after our write
        raise patch_mod.AgentError("PATCH_CONFLICT", "simulated concurrent conflict", source="edit")

    def racing_link(src, dst):
        if Path(dst) == target and not target.exists():
            target.write_bytes(b"external-recreated")  # claims the path before the restore
        return real_link(src, dst)

    monkeypatch.setattr(patch_mod, "atomic_write", failing_second)
    monkeypatch.setattr(patch_mod.os, "link", racing_link)

    if kind == "add":
        patch = _patch_text(
            "*** Add File: a.py", "+created by agent",
            "*** Add File: b.py", "+created by agent b",
        )
    else:
        patch = _patch_text(
            "*** Update File: a.py", "@@", "-old", "+new",
            "*** Update File: b.py", "@@", "-old-b", "+new-b",
        )
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "RECOVERY_UNCERTAIN"
    assert "a.py" in result.error.details.get("rollback_incomplete", [])
    assert target.read_bytes() == b"external-recreated", "the recreated path must not be replaced by the restore"
    trashes = list(tmp_path.glob(".a.py.jinlin-rollback-*"))
    assert len(trashes) == 1 and trashes[0].read_bytes() == b"external-in-window", (
        "the content as it stood at move time (the injected external edit, which is what"
        " breaks ownership) must stay recoverable in the trash"
    )


def test_delete_move_hash_mismatch_restore_is_no_clobber(tmp_path, monkeypatch):
    """Round-8/9: when the moved backup fails the ownership fingerprint, the
    original bytes are re-created at the path from the transaction record and
    the foreign backup is kept aside — no concurrent content is ever deleted
    and the outcome is RECOVERY_UNCERTAIN."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    doomed = tmp_path / "doomed.py"
    doomed.write_text("old\n", encoding="utf-8")
    original_bytes = doomed.read_bytes()
    ctx.state.read_hashes["doomed.py"] = sha(original_bytes)
    real_replace = os.replace

    def racing_replace(src, dst):
        if Path(dst).name.startswith(".doomed.py.jinlin-txn-"):
            doomed.write_bytes(b"external-in-backup")  # rides along, breaks ownership
        return real_replace(src, dst)

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "RECOVERY_UNCERTAIN"
    assert doomed.read_bytes() == original_bytes, (
        "the original content must be re-created at the path from the verified bytes"
    )
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"external-in-backup", (
        "the foreign content stays aside for inspection, never deleted"
    )


def test_delete_backup_cleanup_skips_replaced_backup(tmp_path, monkeypatch):
    """Batch-2 P0: between the verified backup move and the post-commit
    cleanup, an external process can replace the backup path. The cleanup must
    prove ownership (stat fingerprint captured at move time) before deleting:
    a replaced backup is kept, reported as deferred, and the already committed
    transaction stays successful."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_replace = os.replace
    real_stat = os.stat
    armed = {"done": False}
    stats_seen = {"n": 0}
    replaced = {"done": False}

    def racing_replace(src, dst):
        out = real_replace(src, dst)
        if Path(dst).name.startswith(".doomed.py.jinlin-txn-"):
            armed["done"] = True  # the verified backup move completed
        return out

    def racing_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if armed["done"] and Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            stats_seen["n"] += 1
            # stat #1 is the post-move identity check; stat #2 is the cleanup
            # ownership re-check — the replacement slips in there
            if stats_seen["n"] >= 2 and not replaced["done"]:
                replaced["done"] = True
                os.unlink(path)
                Path(path).write_bytes(b"external-claim")
                return real_stat(path, *args, **kwargs)
        return st

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    monkeypatch.setattr(patch_mod.os, "stat", racing_stat)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert result.ok, "a cleanup race must not fail an already committed transaction"
    assert result.extra.get("cleanup_deferred"), "the kept backup must be reported as deferred"
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"external-claim", (
        "the replacement must survive the post-commit cleanup"
    )


def test_delete_backup_cleanup_failure_is_reported_not_fatal(tmp_path, monkeypatch):
    """Batch-2 P0: a backup unlink that fails (proven ours, but the OS refuses)
    must not fail the committed transaction; the leftover is reported as
    deferred cleanup with diagnostics instead of being silently swallowed."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_unlink = os.unlink

    def failing_unlink(path, *args, **kwargs):
        if Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            raise OSError("simulated locked file")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(patch_mod.os, "unlink", failing_unlink)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert result.ok, "cleanup failure must not fail the committed transaction"
    assert result.extra.get("cleanup_deferred"), "the failed cleanup must be reported as deferred"
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1, "the backup must stay on disk when its cleanup failed"


def test_delete_backup_vanished_before_cleanup_is_not_an_error(tmp_path, monkeypatch):
    """Batch-2 P0: a backup that disappears before cleanup needs no delete and
    must not produce any error or deferred entry."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_replace = os.replace
    real_stat = os.stat
    real_unlink = os.unlink
    armed = {"done": False}
    stats_seen = {"n": 0}

    def racing_replace(src, dst):
        out = real_replace(src, dst)
        if Path(dst).name.startswith(".doomed.py.jinlin-txn-"):
            armed["done"] = True
        return out

    def racing_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if armed["done"] and Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            stats_seen["n"] += 1
            if stats_seen["n"] >= 2:
                real_unlink(path)  # external deletes the backup before cleanup
                raise FileNotFoundError(path)
        return st

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    monkeypatch.setattr(patch_mod.os, "stat", racing_stat)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert result.ok
    assert not result.extra.get("cleanup_deferred"), "a vanished backup is not deferred cleanup"
    assert not list(tmp_path.glob(".doomed.py.jinlin-txn-*"))


def test_delete_backup_identity_captured_from_same_object_as_hash(tmp_path, monkeypatch):
    """Round-8/9: the fingerprint comes from the same open file object as the
    content hash. A replacement slipped in at the cleanup re-check must not be
    deleted — the fingerprint mismatch defers the cleanup and keeps it."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_stat = os.stat
    real_replace = os.replace
    armed = {"done": False}
    stats_seen = {"n": 0}
    swapped = {"done": False}

    def racing_replace(src, dst):
        out = real_replace(src, dst)
        if Path(dst).name.startswith(".doomed.py.jinlin-txn-"):
            armed["done"] = True  # the move completed; verification happens next
        return out

    def racing_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if armed["done"] and Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            stats_seen["n"] += 1
            # stat #1 is the post-move identity check; stat #2 is the cleanup
            # re-check — the replacement slips in there and must not be deleted
            if stats_seen["n"] >= 2 and not swapped["done"]:
                swapped["done"] = True
                os.unlink(path)
                Path(path).write_bytes(b"external-claim")
                return real_stat(path, *args, **kwargs)
        return st

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    monkeypatch.setattr(patch_mod.os, "stat", racing_stat)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert result.ok
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"external-claim", (
        "the replacement must not be registered as our backup and then deleted"
    )
    deferred = result.extra.get("cleanup_deferred") or []
    assert any(d["name"] == backups[0].name for d in deferred)


def test_delete_backup_cleanup_stat_error_is_reported_as_deferred(tmp_path, monkeypatch):
    """Round-8 P0: a stat failure is not the same as 'the backup is gone' —
    PermissionError and other I/O errors must surface as deferred cleanup with
    the reason instead of being silently treated as nothing-to-clean."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_stat = os.stat
    real_replace = os.replace
    armed = {"done": False}
    stats_seen = {"n": 0}

    def racing_replace(src, dst):
        out = real_replace(src, dst)
        if Path(dst).name.startswith(".doomed.py.jinlin-txn-"):
            armed["done"] = True
        return out

    def racing_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if armed["done"] and Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            stats_seen["n"] += 1
            # stat #1 is the post-move identity check; stat #2 is the cleanup
            # re-check — the failure is injected at the cleanup re-check
            if stats_seen["n"] >= 2:
                raise PermissionError("simulated EACCES on backup")
        return st

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    monkeypatch.setattr(patch_mod.os, "stat", racing_stat)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    # the new lifecycle reports a cleanup stat failure as deferred cleanup on a
    # committed transaction (commit success and cleanup success are separate)
    assert result.ok
    deferred = result.extra.get("cleanup_deferred") or []
    assert any(
        d["name"].startswith(".doomed.py.jinlin-txn-") and "stat failed" in d["reason"]
        for d in deferred
    ), "a stat failure must be reported as deferred cleanup with its reason"
    assert list(tmp_path.glob(".doomed.py.jinlin-txn-*")), "the backup must stay on disk"


# ------------------------------------------------------------------ P3 create-cleanup boundary matrix (batch 2)


def _armed_create_failure_hooks(monkeypatch):
    """Shared rig: the exclusive create succeeds, then fsync fails; the test
    decides what the world looks like when the cleanup ownership stat runs
    (the second stat of the target after the failure)."""
    import jinlin_agent.tools.patch as patch_mod

    state: dict[str, Any] = {
        "failed": False, "seen": 0, "on_cleanup_stat": None, "target": None, "real_stat": os.stat,
    }

    def failing_fsync(fd):
        state["failed"] = True
        raise OSError("simulated mid-write failure")

    def racing_stat(path, *args, **kwargs):
        real_stat = state["real_stat"]
        st = real_stat(path, *args, **kwargs)
        if state["failed"] and state["target"] is not None and Path(path) == state["target"]:
            state["seen"] += 1
            # the identity fingerprint is taken via fstat(fd), so the FIRST
            # stat of the target after the failure is the ownership check in
            # cleanup — exactly where an external writer slips the swap in
            if state["seen"] >= 1 and state["on_cleanup_stat"] is not None:
                replacement = state["on_cleanup_stat"](path)
                if replacement is not None:
                    return replacement
        return st

    monkeypatch.setattr(patch_mod.os, "fsync", failing_fsync)
    monkeypatch.setattr(patch_mod.os, "stat", racing_stat)
    return state


def test_atomic_create_failure_replaced_same_size_is_not_unlinked(tmp_path, monkeypatch):
    """P3 boundary: an external replacement can reuse the freed inode and even
    match the original byte size — the fingerprint's size+mtime_ns components
    must still tell the two files apart, so the replacement survives."""
    import jinlin_agent.tools.patch as patch_mod

    target = tmp_path / "new.py"
    our_data = b"created by agent\n"  # 17 bytes
    assert len(our_data) == 17
    state = _armed_create_failure_hooks(monkeypatch)
    state["target"] = target

    real_stat = state["real_stat"]

    def claim_same_size(path):
        os.unlink(path)
        target.write_bytes(b"external 17 byte")  # same size, different mtime
        return real_stat(path)

    state["on_cleanup_stat"] = claim_same_size
    with pytest.raises(AgentError) as excinfo:
        patch_mod.atomic_write(target, our_data, create=True)
    assert excinfo.value.code == "RECOVERY_UNCERTAIN"
    assert target.exists() and target.read_bytes() == b"external 17 byte", (
        "a same-size replacement with a different mtime must not be deleted by name"
    )


def test_atomic_create_failure_fingerprint_collision_documents_residual(tmp_path, monkeypatch):
    """P3 boundary (documented residual, verified honestly): an adversary who
    replicates the FULL stat fingerprint — all four components (st_dev,
    st_ino, st_size, st_mtime_ns) — is outside the threat model; the helper
    then treats the path as ours and deletes it. The test explicitly checks
    all four fields after the swap: when the platform reproduces the inode
    reuse the collision is complete and the deletion is asserted; when it
    does not, the replacement survives and that outcome is asserted instead.
    Either way the test records what was actually verified."""
    import jinlin_agent.tools.patch as patch_mod

    target = tmp_path / "new.py"
    our_data = b"created by agent\n"
    state = _armed_create_failure_hooks(monkeypatch)
    state["target"] = target
    observed: dict[str, Any] = {"collision": None}

    real_stat = state["real_stat"]

    def claim_identical_fingerprint(path):
        # capture the four-field fingerprint of the file as it stands (ours),
        # then swap in a replacement and measure how much of it collides
        st = real_stat(path)
        original = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        os.unlink(path)
        target.write_bytes(our_data)  # same bytes → same size
        os.utime(target, ns=(st.st_mtime_ns, st.st_mtime_ns))  # same mtime
        new = real_stat(path)
        replacement = (new.st_dev, new.st_ino, new.st_size, new.st_mtime_ns)
        observed["collision"] = replacement == original  # all four, explicitly
        return new

    state["on_cleanup_stat"] = claim_identical_fingerprint
    with pytest.raises((OSError, AgentError)) as excinfo:
        patch_mod.atomic_write(target, our_data, create=True)
    assert observed["collision"] is not None, "the test must measure the four-field collision"
    if observed["collision"]:
        # documented residual: full fingerprint collision (dev/ino/size/mtime
        # all equal) — the path is treated as ours and deleted, and the
        # original create failure surfaces as-is (not wrapped)
        assert not isinstance(excinfo.value, AgentError)
        assert not target.exists(), (
            "documented residual: a full stat fingerprint collision is indistinguishable from our file"
        )
    else:
        # the platform did not reproduce the identity reuse: the replacement is preserved
        assert isinstance(excinfo.value, AgentError)
        assert excinfo.value.code == "RECOVERY_UNCERTAIN"
        assert target.exists() and target.read_bytes() == our_data


def test_atomic_create_failure_vanished_path_is_reported_uncertain(tmp_path, monkeypatch):
    """P3 boundary: the file is deleted externally before the ownership check
    and never recreated — nothing is deleted by name and the outcome is
    RECOVERY_UNCERTAIN."""
    import jinlin_agent.tools.patch as patch_mod

    target = tmp_path / "new.py"
    state = _armed_create_failure_hooks(monkeypatch)
    state["target"] = target

    def vanish(path):
        os.unlink(path)
        raise FileNotFoundError(path)  # the name is gone when the check runs

    state["on_cleanup_stat"] = vanish
    with pytest.raises(AgentError) as excinfo:
        patch_mod.atomic_write(target, b"created by agent", create=True)
    assert excinfo.value.code == "RECOVERY_UNCERTAIN"
    assert not target.exists()



# ------------------------------------------------------------------ round-9 P0: backup lifecycle


def test_delete_backup_swap_between_move_and_open_is_not_restored(tmp_path, monkeypatch):
    """Round-9 P0: a replacement that lands between the move and the backup
    verification open is detected by the content+fingerprint re-check: it is
    never restored as the original. The original bytes from the transaction
    record are re-created at the path, the replacement stays in the backup,
    and the outcome is RECOVERY_UNCERTAIN."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_bytes(b"old\n")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_open = os.open

    def racing_open(path, *args, **kwargs):
        if (
            isinstance(path, (str, bytes, os.PathLike))
            and Path(path).name.startswith(".doomed.py.jinlin-txn-")
        ):
            # the replacement lands after the move, before the backup is opened
            # for verification
            Path(path).unlink(missing_ok=True)
            Path(path).write_bytes(b"external-claim")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(patch_mod.os, "open", racing_open)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "RECOVERY_UNCERTAIN"
    assert target.read_bytes() == b"old\n", (
        "the original content must be re-created at its path from the recorded bytes"
    )
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"external-claim", (
        "the replacement must be kept aside, never restored as the original"
    )


def test_delete_backup_verification_open_failure_recovers_original(tmp_path, monkeypatch):
    """Round-9 P0: a failure of the verification open happens AFTER the move —
    the pending transaction record must let the rollback re-create the original
    from the recorded bytes (the old lifecycle appended its record only after
    this point, so the path was lost with the backup unaccounted for)."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_bytes(b"old\n")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_open = os.open

    def failing_open(path, *args, **kwargs):
        if (
            isinstance(path, (str, bytes, os.PathLike))
            and Path(path).name.startswith(".doomed.py.jinlin-txn-")
        ):
            raise PermissionError("simulated EACCES on the backup")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(patch_mod.os, "open", failing_open)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert target.exists() and target.read_bytes() == b"old\n", (
        "the original content must be re-created from the pending record"
    )
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1, "the backup stays on disk, accounted for by the rollback"


def test_delete_backup_verification_read_failure_recovers_original(tmp_path, monkeypatch):
    """Round-9 P0: a read failure during the backup verification happens AFTER
    the move — the pending transaction record must let the rollback re-create
    the original from the recorded bytes (the old lifecycle appended its
    record only after this point, so the path was lost)."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_bytes(b"old\n")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_read = os.read
    real_open = os.open
    target_fd = {"fd": None}

    def tracking_open(path, *args, **kwargs):
        fd_ = real_open(path, *args, **kwargs)
        if isinstance(path, (str, bytes, os.PathLike)) and Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            target_fd["fd"] = fd_
        return fd_

    def failing_read(fd, *args, **kwargs):
        if target_fd["fd"] is not None and fd == target_fd["fd"]:
            raise OSError("simulated I/O error during verification read")
        return real_read(fd, *args, **kwargs)

    monkeypatch.setattr(patch_mod.os, "open", tracking_open)
    monkeypatch.setattr(patch_mod.os, "read", failing_read)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert target.exists() and target.read_bytes() == b"old\n", (
        "the original content must be re-created from the pending record"
    )
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1, "the backup stays on disk, accounted for by the rollback"


def test_delete_backup_verification_fstat_failure_recovers_original(tmp_path, monkeypatch):
    """Round-9 P0: an fstat failure during the backup verification happens
    AFTER the move — the pending transaction record must let the rollback
    re-create the original from the recorded bytes."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_bytes(b"old\n")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_fstat = os.fstat
    real_open = os.open
    target_fd = {"fd": None}

    def tracking_open(path, *args, **kwargs):
        fd_ = real_open(path, *args, **kwargs)
        if isinstance(path, (str, bytes, os.PathLike)) and Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            target_fd["fd"] = fd_
        return fd_

    def failing_fstat(fd):
        if target_fd["fd"] is not None and fd == target_fd["fd"]:
            raise OSError("simulated I/O error during verification fstat")
        return real_fstat(fd)

    monkeypatch.setattr(patch_mod.os, "open", tracking_open)
    monkeypatch.setattr(patch_mod.os, "fstat", failing_fstat)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert target.exists() and target.read_bytes() == b"old\n", (
        "the original content must be re-created from the pending record"
    )
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1, "the backup stays on disk, accounted for by the rollback"


def test_delete_backup_stat_failure_after_move_recovers_original(tmp_path, monkeypatch):
    """Round-9 P0: an I/O failure of the post-move identity check leaves the
    backup's ownership unproven — the original content is re-created at its
    path from the verified handle, the backup is kept, and the outcome is
    RECOVERY_UNCERTAIN (the old lifecycle reported the deletion as a success
    with a deferred entry while the path stayed lost)."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    target = tmp_path / "doomed.py"
    target.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["doomed.py"] = sha(target.read_bytes())
    real_replace = os.replace
    real_stat = os.stat
    armed = {"done": False}

    def racing_replace(src, dst):
        out = real_replace(src, dst)
        if Path(dst).name.startswith(".doomed.py.jinlin-txn-"):
            armed["done"] = True
        return out

    def racing_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if armed["done"] and Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            raise OSError("simulated I/O error on the post-move identity check")
        return st

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    monkeypatch.setattr(patch_mod.os, "stat", racing_stat)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "RECOVERY_UNCERTAIN"
    assert target.exists() and target.read_text(encoding="utf-8") == "old\n", (
        "the original content must be re-created when the backup cannot be verified"
    )
    assert list(tmp_path.glob(".doomed.py.jinlin-txn-*")), "the unverifiable backup is kept, not deleted"


def test_rollback_delete_backup_replaced_in_window_is_kept(tmp_path, monkeypatch):
    """Round-9 P0: when a rollback runs, a backup whose fingerprint no longer
    matches the moved object is not provably ours — the original content is
    re-created from the recorded bytes and the foreign backup is kept, never
    linked into the path."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    doomed = tmp_path / "doomed.py"
    other = tmp_path / "other.py"
    doomed.write_text("old\n", encoding="utf-8")
    other.write_text("keep\n", encoding="utf-8")
    ctx.state.read_hashes["doomed.py"] = sha(doomed.read_bytes())
    ctx.state.read_hashes["other.py"] = sha(other.read_bytes())
    real_atomic = patch_mod.atomic_write
    real_stat = os.stat
    calls = {"n": 0}

    def failing_second(path, data, mode_from=None, expected_sha256=None, create=False):
        # the delete op never calls atomic_write, so the first call is the
        # update — make it fail to trigger the rollback of the delete
        raise patch_mod.AgentError("PATCH_CONFLICT", "simulated conflict", source="edit")

    def racing_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        if Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            # during the rollback, the backup presents a foreign fingerprint
            return real_stat(other)
        return st

    monkeypatch.setattr(patch_mod, "atomic_write", failing_second)
    monkeypatch.setattr(patch_mod.os, "stat", racing_stat)
    patch = _patch_text(
        "*** Delete File: doomed.py",
        "*** Update File: other.py",
        "@@",
        "-keep",
        "+kept",
    )
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "RECOVERY_UNCERTAIN"
    assert doomed.read_text(encoding="utf-8") == "old\n", (
        "the original content must be re-created from the recorded bytes"
    )
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1, "the unverifiable backup is kept for inspection, not linked into the path"


def test_delete_backup_replaced_and_original_path_claimed_creates_rescue(tmp_path, monkeypatch):
    """Round-10 P0 acceptance: the backup replaced concurrently AND the path
    claimed by another writer - the original bytes are parked in a rescue
    file (verified, fsync'ed, manifest-recorded) instead of being lost; the
    replacement stays in the backup and the claimant stays at the path."""
    import json
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    doomed = tmp_path / "doomed.py"
    other = tmp_path / "other.py"
    doomed.write_bytes(b"original A bytes\n")
    other.write_bytes(b"keep\n")
    ctx.state.read_hashes["doomed.py"] = sha(doomed.read_bytes())
    ctx.state.read_hashes["other.py"] = sha(other.read_bytes())
    real_atomic = patch_mod.atomic_write
    real_write_exclusive = patch_mod._write_exclusive

    def failing_second(path, data, mode_from=None, expected_sha256=None, create=False):
        raise patch_mod.AgentError("PATCH_CONFLICT", "simulated conflict", source="edit")

    def claiming_exclusive_write(path, data):
        # during the rollback reconstruction, an external writer claims the
        # original path before our O_EXCL create can establish ownership.
        if Path(path) == doomed:
            doomed.write_bytes(b"external C bytes\n")
            raise FileExistsError("destination claimed concurrently")
        return real_write_exclusive(path, data)

    monkeypatch.setattr(patch_mod, "atomic_write", failing_second)
    monkeypatch.setattr(patch_mod, "_write_exclusive", claiming_exclusive_write)
    patch = _patch_text(
        "*** Delete File: doomed.py",
        "*** Update File: other.py",
        "@@",
        "-keep",
        "+kept",
    )
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "RECOVERY_UNCERTAIN"
    assert doomed.read_bytes() == b"external C bytes\n", "the claimant must not be overwritten"
    rescues = list(tmp_path.glob(".doomed.py.jinlin-rescue-*"))
    assert len(rescues) == 1, "the original bytes must be persisted in a rescue file"
    assert rescues[0].read_bytes() == b"original A bytes\n", "the rescue holds the verified original bytes"
    assert "rescue" in json.dumps(result.error.details.get("rescued_files", [])), (
        "the rescue location must be reported in the error details"
    )
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1, "the unverified backup is kept aside as well"

def test_write_exclusive_survives_short_write(tmp_path, monkeypatch):
    """Round-10 P0: os.write may complete partially — the exclusive write used
    for materialization and rescue files loops until every byte is persisted
    and fsync'ed, so a partial first write never yields a truncated file."""
    import jinlin_agent.tools.patch as patch_mod

    target = tmp_path / "f.bin"
    data = b"x" * 1000
    real_write = os.write

    def short_writing_write(fd, buf):
        if len(buf) > 4:
            return real_write(fd, buf[: len(buf) // 2])
        return real_write(fd, buf)

    monkeypatch.setattr(patch_mod.os, "write", short_writing_write)
    patch_mod._write_exclusive(target, data)
    assert target.read_bytes() == data, "short writes must be completed by the write loop"
    assert len(list(tmp_path.glob("f.bin*"))) == 1
