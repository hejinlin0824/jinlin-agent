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
    assert target.read_bytes() == b"external-doomed"


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
    """Round-4 P1-2: when the moved-aside backup fails ownership verification,
    putting the original back must also be create-if-absent. A path recreated
    concurrently is kept, the original content stays in the backup, and the
    outcome is RECOVERY_UNCERTAIN — not an overwriting restore."""
    import jinlin_agent.tools.patch as patch_mod

    ctx = make_ctx(tmp_path)
    doomed = tmp_path / "doomed.py"
    doomed.write_text("old\n", encoding="utf-8")
    ctx.state.read_hashes["doomed.py"] = sha(doomed.read_bytes())

    real_replace = os.replace
    real_link = os.link

    def racing_replace(src, dst):
        src_p, dst_p = Path(src), Path(dst)
        if dst_p.name.startswith(".doomed.py.jinlin-txn-"):
            doomed.write_bytes(b"external-in-backup")  # rides along, breaks ownership
        elif src_p.name.startswith(".doomed.py.jinlin-txn-") and dst_p == doomed:
            doomed.write_bytes(b"external-recreated")  # old design: recreated before restore
        return real_replace(src, dst)

    def racing_link(src, dst):
        if Path(dst) == doomed and not doomed.exists():
            doomed.write_bytes(b"external-recreated")  # claims the path before the restore
        return real_link(src, dst)

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    monkeypatch.setattr(patch_mod.os, "link", racing_link)
    patch = _patch_text("*** Delete File: doomed.py")
    result = patch_mod.apply_patch(ctx, {"patch": patch})
    assert not result.ok
    assert result.error is not None and result.error.code == "RECOVERY_UNCERTAIN"
    assert doomed.read_bytes() == b"external-recreated", "the recreated path must not be replaced"
    backups = list(tmp_path.glob(".doomed.py.jinlin-txn-*"))
    assert len(backups) == 1 and backups[0].read_bytes() == b"external-in-backup", (
        "the content as it stood at move time (the injected external edit, which is what"
        " breaks ownership) must stay recoverable in the backup"
    )


# ------------------------------------------------------------------ P2 strict UTF-8 (round 4 rider)


def test_invalid_utf8_inside_event_is_rejected(tmp_path):
    """Strict UTF-8 decoding: invalid bytes inside a JSON string value must not
    be silently replaced and pass as a valid event — reading and EventLog.open()
    must both fail closed."""
    good = (_evt(1) + "\n").encode()
    bad = b'{"schema_version":1,"seq":2,"type":"x","payload":{"note":"\xff\xfe"}}\n'
    _write_bytes(tmp_path / "events.jsonl", good + bad)
    with pytest.raises(AgentError) as excinfo:
        list(read_events(tmp_path))
    assert excinfo.value.code == "SESSION_CORRUPTED"
    with pytest.raises(AgentError) as excinfo:
        EventLog(tmp_path, "sess_utf8").open()
    assert excinfo.value.code == "SESSION_CORRUPTED"


# ------------------------------------------------------------------ P2 security hardening (phase 2)


def test_restore_no_clobber_does_not_unlink_recreated_source(tmp_path, monkeypatch):
    """Round-6 P2: between os.link and an unconditional os.unlink, an external
    process can replace the source path; deleting by name then removes the
    replacement. Plan A: the helper never unlinks — the restored content is
    safe at dest, and the redundant source entry is left intact for
    inspection (reported as redundant trash by the caller)."""
    import jinlin_agent.tools.patch as patch_mod

    source = tmp_path / ".a.py.jinlin-rollback-deadbeef"
    dest = tmp_path / "a.py"
    source.write_bytes(b"ours")
    real_unlink = os.unlink
    calls = {"unlink": 0}

    def racing_unlink(path, *args, **kwargs):
        if Path(path) == source:
            calls["unlink"] += 1
            source.write_bytes(b"external-recreated-source")  # external claims the name
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(patch_mod.os, "unlink", racing_unlink)
    assert patch_mod._restore_no_clobber(source, dest) is True
    assert calls["unlink"] == 0, "restore must never unlink the source path by name"
    assert dest.read_bytes() == b"ours"
    assert source.exists() and source.read_bytes() == b"ours", (
        "the redundant source entry must be left intact for inspection"
    )


def test_open_rejects_mismatched_session_id(tmp_path):
    """P2-2: a log belonging to another session must not be accepted and
    appended to by this instance; the lock must be released on failure so the
    owning session can still open its log."""
    _write_events(
        tmp_path / "events.jsonl",
        [json.dumps({"schema_version": 1, "seq": 1, "type": "session.created", "payload": {}, "session_id": "sess_other"})],
    )
    with pytest.raises(AgentError) as excinfo:
        EventLog(tmp_path, "sess_mine").open()
    assert excinfo.value.code == "SESSION_CORRUPTED"
    second = EventLog(tmp_path, "sess_other")
    second.open()
    try:
        assert second.append("step.started")["seq"] == 2
    finally:
        second.close()
    events = list(read_events(tmp_path))
    assert [e["seq"] for e in events] == [1, 2]


def test_legacy_log_without_session_id_migrates_on_append(tmp_path):
    """P2 (batch 2): a log whose events all lack session_id is the documented
    legacy class — opening it for append is allowed and the appended events
    carry the opening session's id (the explicit migration path)."""
    _write_events(tmp_path / "events.jsonl", [_evt(1)])
    log = EventLog(tmp_path, "sess_x").open()
    try:
        log.append("step.started")
    finally:
        log.close()
    events = list(read_events(tmp_path))
    assert [e["seq"] for e in events] == [1, 2]
    assert events[1]["session_id"] == "sess_x"  # migrated suffix is attributed


def test_mixed_log_attributed_then_unattributed_is_rejected(tmp_path):
    """P2 (batch 2): once a log carries attributed events, a later event
    without session_id is a shape our writer can never produce — reading and
    opening must fail closed instead of silently accepting a mixed log."""
    first = json.dumps({"schema_version": 1, "seq": 1, "type": "x", "payload": {}, "session_id": "sess_a"})
    second = json.dumps({"schema_version": 1, "seq": 2, "type": "y", "payload": {}})
    _write_events(tmp_path / "events.jsonl", [first, second])
    with pytest.raises(AgentError) as excinfo:
        list(read_events(tmp_path))
    assert excinfo.value.code == "SESSION_CORRUPTED"
    with pytest.raises(AgentError) as excinfo:
        EventLog(tmp_path, "sess_a").open()
    assert excinfo.value.code == "SESSION_CORRUPTED"


def test_atomic_create_failure_does_not_unlink_replaced_path(tmp_path, monkeypatch):
    """P2-3: when an exclusive create fails midway and the path no longer
    provably holds the file this call created, cleanup must not delete by
    name — the foreign content stays and the outcome is RECOVERY_UNCERTAIN."""
    import jinlin_agent.tools.patch as patch_mod

    target = tmp_path / "new.py"
    real_stat = os.stat
    real_unlink = os.unlink
    failed = {"done": False}
    replaced = {"done": False}

    def failing_fsync(fd):
        failed["done"] = True
        raise OSError("simulated mid-write failure")

    def racing_stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        # once the write failed and our fd is closed, an external writer
        # replaces the path with a different inode before cleanup runs
        if failed["done"] and not replaced["done"] and Path(path) == target:
            replaced["done"] = True
            real_unlink(path)
            target.write_bytes(b"external-partial-claim")
            return real_stat(path, *args, **kwargs)
        return st

    def racing_unlink(path, *args, **kwargs):
        # old design: cleanup deletes by name and removes the replacement
        if Path(path) == target:
            target.write_bytes(b"external-partial-claim")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(patch_mod.os, "fsync", failing_fsync)
    monkeypatch.setattr(patch_mod.os, "stat", racing_stat)
    monkeypatch.setattr(patch_mod.os, "unlink", racing_unlink)
    with pytest.raises(AgentError) as excinfo:
        patch_mod.atomic_write(target, b"created by agent", create=True)
    assert excinfo.value.code == "RECOVERY_UNCERTAIN"
    assert target.exists() and target.read_bytes() == b"external-partial-claim", (
        "cleanup must not delete a path that no longer holds our file"
    )


def test_atomic_create_failure_cleans_own_partial_file(tmp_path, monkeypatch):
    """P2-3 (identity match): when the path still provably holds the file this
    call created, the partial file is removed and the original error surfaces."""
    import jinlin_agent.tools.patch as patch_mod

    target = tmp_path / "new.py"

    def failing_fsync(fd):
        raise OSError("simulated mid-write failure")

    monkeypatch.setattr(patch_mod.os, "fsync", failing_fsync)
    with pytest.raises(OSError):
        patch_mod.atomic_write(target, b"created by agent", create=True)
    assert not target.exists(), "our own partial file must be cleaned up"


# ------------------------------------------------------------------ P0 delete-backup commit cleanup (batch 2)


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
    real_unlink = os.unlink
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
            # first stat after the move is the identity capture; the cleanup
            # stat (second) is where the external writer slips the replacement in
            if stats_seen["n"] >= 2 and not replaced["done"]:
                replaced["done"] = True
                real_unlink(path)
                Path(path).write_bytes(b"external-claim")
                return real_stat(path, *args, **kwargs)
        return st

    def racing_unlink(path, *args, **kwargs):
        # old design: cleanup deletes by name and removes the replacement
        if Path(path).name.startswith(".doomed.py.jinlin-txn-"):
            Path(path).write_bytes(b"external-claim")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(patch_mod.os, "replace", racing_replace)
    monkeypatch.setattr(patch_mod.os, "stat", racing_stat)
    monkeypatch.setattr(patch_mod.os, "unlink", racing_unlink)
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
    """P3 boundary (documented residual): an adversary who replicates the full
    stat fingerprint (size AND mtime_ns) is outside the threat model — the
    helper then treats the path as ours and deletes it, and the original
    failure surfaces. This pins the honest limit of a stat-based identity
    instead of claiming atomic identity deletion."""
    import jinlin_agent.tools.patch as patch_mod

    target = tmp_path / "new.py"
    our_data = b"created by agent\n"
    state = _armed_create_failure_hooks(monkeypatch)
    state["target"] = target

    real_stat = state["real_stat"]

    def claim_identical_fingerprint(path):
        # capture the fingerprint of the file as it stands (ours), then swap
        # in a replacement carrying that exact fingerprint
        st = real_stat(path)
        target.write_bytes(our_data)  # same bytes → same size
        os.utime(target, ns=(st.st_mtime_ns, st.st_mtime_ns))
        return real_stat(path)

    state["on_cleanup_stat"] = claim_identical_fingerprint
    with pytest.raises(OSError):
        patch_mod.atomic_write(target, our_data, create=True)
    assert not target.exists(), (
        "documented residual: an identical stat fingerprint is indistinguishable from our file"
    )


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

