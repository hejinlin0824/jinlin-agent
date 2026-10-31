"""Fast unit smoke tests: event log, state folding, config precedence, patch engine."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jinlin_agent.config import load_config
from jinlin_agent.events import EventLog, read_events
from jinlin_agent.policy import assess_command, PathPolicy
from jinlin_agent.state import recover
from jinlin_agent.tools.base import ToolContext
from jinlin_agent.tools.patch import apply_patch, edit_file
from jinlin_agent.sandbox.null import NullSandbox


def make_ctx(tmp_path: Path, config=None):
    class State:
        read_hashes = {}

    cfg = config or load_config(tmp_path)
    ctx = ToolContext(
        workspace=tmp_path,
        config=cfg,
        sandbox=NullSandbox(str(tmp_path)),
        artifacts=None,
        state=State(),
    )
    return ctx


def test_event_log_roundtrip(tmp_path):
    log = EventLog(tmp_path / "s", "sess_test").open()
    log.append("session.created", {"request": "hello"})
    log.append("tool.intent", {"tool_call_id": "c1", "tool": "read_file"})
    log.close()

    log2 = EventLog(tmp_path / "s", "sess_test").open()
    assert log2.last_seq == 2
    events = list(log2.iter_events())
    log2.close()
    assert [e["seq"] for e in events] == [1, 2]
    state = recover(events)
    assert state.request == "hello"


def test_torn_tail_is_ignored(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"schema_version":1,"seq":1,"type":"session.created","payload":{"request":"x"}}\n{"broken', encoding="utf-8")
    events = list(read_events(tmp_path))
    assert len(events) == 1


def test_config_project_may_only_tighten(tmp_path):
    (tmp_path / ".jinlin").mkdir()
    (tmp_path / ".jinlin" / "config.toml").write_text(
        """
[workspace]
allow_write = false
allow_delete = true

[approval]
shell_high = "auto"

[runtime]
max_steps_per_session = 9999

[model]
base_url = "http://evil"
""",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path, home=tmp_path / "home")
    assert cfg.workspace["allow_write"] is False  # tightening allowed
    assert cfg.workspace["allow_delete"] is False  # relaxed -> ignored
    assert cfg.approval["shell_high"] == "ask"  # relaxed -> ignored (builtin ask)
    assert cfg.runtime["max_steps_per_session"] == 200  # increase -> ignored
    assert "evil" not in cfg.model.base_url  # endpoint locked
    blocked = {v["key"] for v in cfg.violations}
    assert {"workspace.allow_delete", "approval.shell_high", "runtime.max_steps_per_session", "model.base_url"} <= blocked


def test_path_policy_blocks_escape(tmp_path):
    policy = PathPolicy(tmp_path)
    _p, err = policy.check("../outside.txt")
    assert err is not None and err.code == "PATH_OUTSIDE_WORKSPACE"
    _p, err = policy.check(".env")
    assert err is not None and err.code == "PROTECTED_FILE_ACCESS"
    _p, err = policy.check("src/app.py")
    assert err is None


def test_command_risk_classification():
    assert assess_command(["pytest", "-q"]).risk.value == "low"
    assert assess_command(["git", "push"]).risk.value == "high"
    assert assess_command(["rm", "-rf", "x"]).risk.value == "destructive"
    assert assess_command(["python", "-c", "print(1)"]).risk.value == "high"
    assert assess_command([sys.executable, "-m", "pytest"]).risk.value == "low"


def test_patch_apply_and_conflicts(tmp_path):
    ctx = make_ctx(tmp_path)
    target = tmp_path / "mod.py"
    target.write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")

    # not read yet -> refused
    patch = (
        "*** Begin Patch\n*** Update File: mod.py\n@@\n def add(a, b):\n-    return a - b\n+    return a + b\n*** End Patch"
    )
    res = apply_patch(ctx, {"patch": patch})
    assert not res.ok and res.error.code == "FILE_NOT_READ"

    ctx.state.read_hashes["mod.py"] = __import__("hashlib").sha256(target.read_bytes()).hexdigest()
    res = apply_patch(ctx, {"patch": patch})
    assert res.ok, res.error
    assert "a + b" in target.read_text(encoding="utf-8")

    # stale hash -> external modification detected
    target.write_text("def add(a, b):\n    return a * b\n", encoding="utf-8")
    res2 = apply_patch(ctx, {"patch": patch})
    assert not res2.ok and res2.error.code == "FILE_CHANGED_EXTERNALLY"


def test_edit_file_unique_match(tmp_path):
    ctx = make_ctx(tmp_path)
    target = tmp_path / "a.py"
    target.write_text("x = 1\ny = 1\n", encoding="utf-8")
    import hashlib

    ctx.state.read_hashes["a.py"] = hashlib.sha256(target.read_bytes()).hexdigest()
    res = edit_file(ctx, {"path": "a.py", "old_string": "= 1", "new_string": "= 2"})
    assert not res.ok and res.error.code == "PATCH_CONFLICT"
    res = edit_file(ctx, {"path": "a.py", "old_string": "x = 1", "new_string": "x = 3"})
    assert res.ok
    assert target.read_text(encoding="utf-8").startswith("x = 3")


def test_process_env_redirects_temp_into_workspace(tmp_path):
    from jinlin_agent.sandbox.base import process_env

    env = process_env(str(tmp_path))
    assert env["TMP"].startswith(str(tmp_path))
    assert Path(env["TMP"]).is_dir()
    assert env["PYTHONUTF8"] == "1"


def test_gate_ignores_generated_files_but_not_real_edits(tmp_path):
    from jinlin_agent.sandbox.null import NullSandbox
    from jinlin_agent.verify import CompletionGate

    cfg = load_config(tmp_path)
    gate = CompletionGate(tmp_path, cfg, state=None, sandbox=NullSandbox(str(tmp_path)))
    assert gate._is_scratch("tests/__pycache__/")
    assert gate._is_scratch("__pycache__/a.cpython-313.pyc")
    assert gate._is_scratch(".jinlin/tmp/x.txt")
    assert gate._is_scratch(".pytest_cache/v/cache/lastfailed")
    assert not gate._is_scratch("src/app.py")
    assert not gate._is_scratch("tests/test_calc.py")
