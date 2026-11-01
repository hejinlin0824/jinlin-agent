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

