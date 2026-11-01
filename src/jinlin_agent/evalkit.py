"""Evaluation kit: conformance probes (T1-T4) and task evaluations (E1-E8).

Everything here is deterministic and offline: scripted providers, tiny fixture
repositories, assertions over the event log and the real workspace. This is what
backs the claim "it is a harness, and it is not a toy".
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .config import load_config
from .domain import Status
from .model.mock import MockProvider
from .sandbox.null import NullSandbox
from .session import SessionManager
from .state import recover

PY = sys.executable


# --------------------------------------------------------------------------- fixtures


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _test_config(root: Path, timeout: int = 120) -> None:
    _write(
        root,
        ".jinlin/config.toml",
        f'[verification]\ntest_command = ["{PY.replace(chr(92), "/")}", "-m", "pytest", "-q"]\n'
        f"baseline_timeout_seconds = {timeout}\n",
    )


def make_fixture(root: Path, kind: str) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    if kind == "bugfix":
        _write(root, "calc.py", "def add(a, b):\n    return a - b\n")
        _write(root, "tests/test_calc.py", "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n")
        _write(root, "conftest.py", "")
        _write(root, "pyproject.toml", '[project]\nname = "calc"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n')
        _write(root, "README.md", "# calc\n\nA tiny calculator.\n")
        _test_config(root)
    elif kind == "feature":
        _write(root, "config.py", 'SETTINGS = {\n    "retries": 3,\n}\n')
        _write(root, "loader.py", "from config import SETTINGS\n\n\ndef load(name):\n    return SETTINGS[name]\n")
        _write(root, "tests/test_loader.py", "from loader import load\n\n\ndef test_retries():\n    assert load('retries') == 3\n")
        _write(root, "conftest.py", "")
        _write(root, "pyproject.toml", '[project]\nname = "feature"\nversion = "0.1.0"\n')
        _write(root, "README.md", "# feature\n\nSettings loader.\n")
        _test_config(root)
    elif kind == "docs":
        _write(root, "README.md", "# service\n\nEntry point: `app.main()` starts the server.\n")
        _write(root, "app.py", "def main():\n    return 'served'\n")
        _write(root, "docs/design.md", "The request path is main -> handler.\n")
    else:
        raise ValueError(kind)
    return root


# --------------------------------------------------------------------------- runner


@dataclass
class RunOutcome:
    session: Any
    provider: MockProvider
    status: str
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""


def run_scripted(
    workspace: Path,
    home: Path,
    task: str,
    script: list[dict[str, Any]],
    *,
    max_steps: int = 40,
    read_only: bool = False,
    auto_approve: bool = True,
    approval_handler: Callable[[Any], tuple[str, str]] | None = None,
) -> RunOutcome:
    cfg = load_config(workspace, home=home)
    cfg.runtime["max_steps_per_session"] = max_steps
    cfg.runtime["max_model_calls"] = max_steps + 2
    sandbox = NullSandbox(str(workspace))
    from .loop import Harness

    session = SessionManager.create(cfg, workspace, task, sandbox)
    provider = MockProvider(script)
    harness = Harness(
        session=session,
        config=cfg,
        provider=provider,
        approval_handler=approval_handler,
        auto_approve=auto_approve and approval_handler is None,
        read_only=read_only,
        log=lambda msg: None,
    )
    try:
        status = harness.run()
        events = list(session.log.iter_events())
    finally:
        session.close()
    return RunOutcome(session=session, provider=provider, status=status, events=events)


def _events_of(outcome: RunOutcome, etype: str) -> list[dict[str, Any]]:
    return [e for e in outcome.events if e.get("type") == etype]


def _has(outcome: RunOutcome, etype: str, **match: Any) -> bool:
    for evt in _events_of(outcome, etype):
        payload = evt.get("payload") or {}
        if all(payload.get(k) == v for k, v in match.items()):
            return True
    return False


def _tool_messages(provider: MockProvider, index: int) -> list[str]:
    if index >= len(provider.requests):
        return []
    return [m.content for m in provider.requests[index].messages if m.role == "tool"]


# --------------------------------------------------------------------------- HCS probes


def probe_t1_loop(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "docs")
    script = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "identify the entry point",
                            "steps": [{"description": "read README", "status": "completed", "evidence": ["README.md"]}],
                        }
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {
                        "summary": "read-only analysis finished",
                        "claims": [
                            {"statement": "analysis: entry point is app.main", "evidence_refs": ["README.md"]}
                        ],
                    },
                }
            ]
        },
    ]
    out = run_scripted(repo, tmp / "home", "find the entry point", script)
    # the loop fed observations back: later requests contain earlier tool results
    later = _tool_messages(out.provider, 1)
    if not any("project_summary" in m for m in later):
        return False, "second model request did not contain the first observation"
    if out.session.state.model_calls < 4:
        return False, f"loop stopped early (model calls={out.session.state.model_calls})"
    if out.session.state.status != Status.COMPLETED.value:
        return False, f"status={out.session.state.status}, expected completed"
    return True, "adaptive loop with observation feedback"


def probe_t2_environment(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "bugfix")
    script = _bugfix_script()
    out = run_scripted(repo, tmp / "home", "fix add()", script)
    body = (repo / "calc.py").read_text(encoding="utf-8")
    if "return a + b" not in body:
        return False, "file was not changed on disk"
    if out.session.state.status != Status.COMPLETED.value:
        return False, f"status={_failure_digest(out)}"
    return True, "tool interface really changed the external environment"


def _failure_digest(out: RunOutcome) -> str:
    """Compact, CI-friendly explanation of why a scripted session did not complete."""
    state = out.session.state
    error = state.last_error or {}
    rejections = [
        evt["payload"].get("reasons", []) for evt in out.events if evt.get("type") == "completion.rejected"
    ]
    parts = [f"status={state.status}", f"error={error.get('code')}: {str(error.get('message', ''))[:120]}"]
    if rejections:
        parts.append("last_rejection=" + "; ".join(str(r) for r in rejections[-1][:2]))
    verifications = []
    for run in state.verification_runs[-4:]:
        detail = f"{run.level}={run.status}"
        if run.exit_code is not None:
            detail += f"(exit={run.exit_code})"
        if run.findings:
            detail += "[" + "; ".join(run.findings[:2])[:200] + "]"
        verifications.append(detail)
    if verifications:
        parts.append("verification=" + " ".join(verifications))
    failed_tools = [
        f"{evt['payload'].get('tool')}:{(evt['payload'].get('error') or {}).get('code')}"
        for evt in out.events
        if evt.get("type") == "tool.failed"
    ]
    if failed_tools:
        parts.append("failed_tools=" + ",".join(failed_tools[-5:]))
    return " | ".join(parts)


def probe_t3_context(tmp: Path) -> tuple[bool, str]:
    """Content-aware selection + budget + compaction, no silent truncation."""
    from .artifacts import ArtifactStore
    from .context import ContextBuilder
    from .domain import Message, Phase
    from .model.base import Capabilities
    from .sandbox.null import NullSandbox
    from .state import SessionState
    from .verify import detect_project

    repo = make_fixture(tmp / "repo", "bugfix")
    cfg = load_config(repo, home=tmp / "home")
    artifacts = ArtifactStore(tmp / "artifacts")
    state = SessionState(session_id="s", workspace=str(repo), request="fix add()", phase=Phase.IMPLEMENTING.value)
    state.plan.objective = "fix add()"
    from .domain import PlanStep

    state.plan.steps = [PlanStep(step_id="s1", description="fix", status="completed", evidence=["calc.py"])]
    history = [Message(role="assistant", content=f"step {i} observation " + "x" * 400) for i in range(40)]
    state.recent_observations = [{"tool": "read_file", "preview": "y" * 300} for _ in range(20)]
    builder = ContextBuilder(
        config=cfg,
        capabilities=Capabilities(context_window=6000, max_output_tokens=1000),
        artifacts=artifacts,
        sandbox=NullSandbox(str(repo)),
        project_instructions="",
    )
    bundle = builder.build(state, history, "fix add()")
    if not bundle.compacted:
        return False, "history was not compacted under budget pressure"
    if not any(b.block_id == "task" for b in bundle.blocks):
        return False, "required task block was dropped"
    if not bundle.messages_artifact_id:
        return False, "messages snapshot artifact missing (T3 audit trail)"
    dropped_reasons = {d.get("reason") for d in bundle.dropped}
    if "compacted" not in dropped_reasons and "budget" not in dropped_reasons:
        return False, "no recorded inclusion/exclusion decisions"
    # every non-required block must carry a reason
    if any(not b.reason for b in bundle.blocks):
        return False, "a context block entered without a recorded reason"
    return True, "content-aware budgeting, compaction and recorded decisions"


def probe_t3_truncation(tmp: Path) -> tuple[bool, str]:
    """Long tool output must be stored as an artifact with explicit truncation metadata."""
    from .artifacts import ArtifactStore
    from .runtime import ToolDispatcher
    from .tools.base import ToolContext
    from .tools.registry import build_default_registry

    repo = make_fixture(tmp / "repo", "bugfix")
    cfg = load_config(repo, home=tmp / "home")
    artifacts = ArtifactStore(tmp / "artifacts")
    big = repo / "big.txt"
    big.write_text(("x" * 100 + "\n") * 4000, encoding="utf-8")
    ctx = ToolContext(workspace=repo, config=cfg, sandbox=NullSandbox(str(repo)), artifacts=artifacts, state=None)
    events: list[dict[str, Any]] = []

    def emit(etype: str, payload: dict[str, Any], turn: str = "", step: str = "") -> dict[str, Any]:
        events.append({"type": etype, "payload": payload})
        return {}

    disp = ToolDispatcher(build_default_registry(), _AllowAll(), artifacts, emit, ctx, cfg)
    from .domain import ToolCallRequest

    call = ToolCallRequest(
        id="c1",
        name="read_file",
        arguments='{"path":"big.txt","limit":2000}',
        parsed={"path": "big.txt", "limit": 2000},
    )
    res = disp.execute(call)
    completed = [e for e in events if e["type"] == "tool.completed"]
    if not completed:
        return False, "read_file did not complete"
    payload = completed[0]["payload"]
    if not payload.get("truncated"):
        return False, "large output was not truncated"
    if not payload.get("artifact_id"):
        return False, "full output was not persisted as an artifact"
    if "output truncated" not in payload.get("output_preview", ""):
        return False, "truncation marker missing from model-facing text"
    return True, "long output -> artifact + transparent truncation"


class _AllowAll:
    class approvals:
        @staticmethod
        def decide(*args, **kwargs):
            return "approved_once", ""

    def evaluate(self, *args, **kwargs):
        from .policy import PolicyDecision

        return PolicyDecision(allowed=True)


def probe_t4_false_success(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "bugfix")
    script = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "fix add()",
                            "steps": [{"description": "look at code", "status": "completed", "evidence": ["calc.py"]}],
                        }
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {
                        "summary": "fixed",
                        "claims": [{"statement": "tests pass now", "evidence_refs": ["calc.py"]}],
                    },
                }
            ]
        },
        {"tool_calls": [{"name": "request_user_input", "arguments": {"question": "still failing"}}]},
    ]
    out = run_scripted(repo, tmp / "home", "fix add()", script)
    rejects = _events_of(out, "completion.rejected")
    if not rejects:
        return False, "the gate accepted a false completion"
    if out.session.state.status == Status.COMPLETED.value:
        return False, "session completed despite failing tests"
    reason_text = " ".join(rejects[0]["payload"].get("reasons", []))
    if "did not pass" not in reason_text and "tests" not in reason_text:
        return False, f"rejection reason not evidence-based: {reason_text[:200]}"
    return True, "false success rejected by the completion gate"


def probe_t4_path_escape(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "bugfix")
    script = [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "../../../etc/passwd"}}]},
        {"tool_calls": [{"name": "request_user_input", "arguments": {"question": "blocked"}}]},
    ]
    out = run_scripted(repo, tmp / "home", "read outside", script)
    denied = _events_of(out, "tool.denied")
    if not denied:
        return False, "path escape was not denied"
    code = (denied[0]["payload"].get("error") or {}).get("code")
    if code != "PATH_OUTSIDE_WORKSPACE":
        return False, f"wrong denial code: {code}"
    return True, "path escape blocked before any side effect"


def probe_t4_loop(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "bugfix")
    read = {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]}
    script = [
        read,
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
        {"tool_calls": [{"name": "request_user_input", "arguments": {"question": "looping"}}]},
    ]
    out = run_scripted(repo, tmp / "home", "loop test", script)
    if not _events_of(out, "loop.detected"):
        return False, "repeated identical tool calls were not detected"
    return True, "loop detection fires and injects a correction"


def probe_t4_approval(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "bugfix")
    script = [
        {"tool_calls": [{"name": "run_command", "arguments": {"command": ["git", "commit", "-m", "x"], "cwd": "."}}]},
        {"tool_calls": [{"name": "request_user_input", "arguments": {"question": "denied"}}]},
    ]
    out = run_scripted(repo, tmp / "home", "push", script, auto_approve=False)
    denied = _events_of(out, "tool.denied")
    if not denied:
        return False, "high-risk command was not denied without an answerer"
    code = (denied[0]["payload"].get("error") or {}).get("code")
    if code != "APPROVAL_DENIED":
        return False, f"wrong denial code: {code}"
    if not _events_of(out, "approval.requested"):
        return False, "approval request was not recorded"
    return True, "fail-closed approval for high-risk commands"


def probe_t5_recovery(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "docs")
    home = tmp / "home"
    cfg = load_config(repo, home=home)
    sandbox = NullSandbox(str(repo))
    session = SessionManager.create(cfg, repo, "recover me", sandbox)
    # simulate a crash between intent and result
    session.emit("tool.intent", {"tool_call_id": "call_x", "tool": "write_file", "args": {"path": "x.txt"}})
    sid = session.id
    session.close()

    reopened = SessionManager.open(cfg, sid, sandbox)
    events = list(reopened.log.iter_events())
    reopened.close()
    recovered = [e for e in events if e["type"] == "tool.failed" and (e["payload"].get("error") or {}).get("code") == "RECOVERY_UNCERTAIN"]
    if not recovered:
        return False, "orphan tool intent was not surfaced on resume"
    return True, "resume flags uncertain side effects instead of guessing"


def probe_t6_evidence(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "bugfix")
    script = _bugfix_script(bogus_evidence=True)
    out = run_scripted(repo, tmp / "home", "fix add()", script)
    rejects = _events_of(out, "completion.rejected")
    if not rejects:
        return False, "completion with bogus evidence was accepted"
    text = " ".join(rejects[0]["payload"].get("reasons", []))
    if "unknown evidence" not in text:
        return False, f"expected unknown-evidence rejection, got: {text[:200]}"
    return True, "claims must reference real evidence"


# --------------------------------------------------------------------------- E1-E8


def _bugfix_script(bogus_evidence: bool = False) -> list[dict[str, Any]]:
    evidence = ["does-not-exist.py"] if bogus_evidence else ["calc.py", "tests/test_calc.py", "L3 tests"]
    return [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "tests/test_calc.py"}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "calc.py"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "fix add()",
                            "steps": [
                                {"description": "read test and implementation", "status": "completed", "evidence": ["calc.py"]},
                                {"description": "fix the operator", "status": "in_progress"},
                            ],
                            "verification_strategy": ["pytest -q"],
                        }
                    },
                }
            ]
        },
        {"tool_calls": [{"name": "edit_file", "arguments": {"path": "calc.py", "old_string": "return a - b", "new_string": "return a + b"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "fix add()",
                            "steps": [
                                {"description": "read test and implementation", "status": "completed", "evidence": ["calc.py"]},
                                {"description": "fix the operator", "status": "completed", "evidence": ["calc.py"]},
                            ],
                        }
                    },
                }
            ]
        },
        {"tool_calls": [{"name": "run_command", "arguments": {"command": [PY, "-m", "pytest", "-q"], "cwd": "."}}]},
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {
                        "summary": "add() now returns the sum; the failing test passes",
                        "claims": [{"statement": "add() returns a + b and the test passes", "evidence_refs": evidence}],
                    },
                }
            ]
        },
    ]


def eval_e2_bugfix(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "bugfix")
    out = run_scripted(repo, tmp / "home", "fix add()", _bugfix_script())
    if out.session.state.status != Status.COMPLETED.value:
        return False, f"status={out.session.state.status}"
    if "return a + b" not in (repo / "calc.py").read_text(encoding="utf-8"):
        return False, "code not fixed"
    if not (out.session.dir / "report.md").exists():
        return False, "final report missing"
    if not _has(out, "verification.completed") and not _events_of(out, "verification.completed"):
        return False, "no verification recorded"
    return True, "verified fix end to end"


def eval_e1_readonly(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "docs")
    script = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "app.py"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "explain the entry point",
                            "steps": [{"description": "read app.py", "status": "completed", "evidence": ["app.py"]}],
                        }
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {
                        "summary": "read-only analysis",
                        "claims": [{"statement": "entry point is app.main()", "evidence_refs": ["app.py"]}],
                    },
                }
            ]
        },
    ]
    out = run_scripted(repo, tmp / "home", "what is the entry point", script)
    if out.session.state.status != Status.COMPLETED.value:
        return False, f"status={out.session.state.status}"
    if out.session.state.written_files:
        return False, "read-only task modified files"
    return True, "read-only analysis completes without writing"


def eval_e3_cross_file(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "feature")
    script = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "config.py"}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "loader.py"}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "tests/test_loader.py"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "add timeout setting",
                            "steps": [
                                {"description": "read config and loader", "status": "completed", "evidence": ["config.py"]},
                                {"description": "add timeout default", "status": "in_progress"},
                                {"description": "test it", "status": "pending"},
                            ],
                        }
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "apply_patch",
                    "arguments": {
                        "patch": (
                            "*** Begin Patch\n"
                            "*** Update File: config.py\n@@\n SETTINGS = {\n     \"retries\": 3,\n+    \"timeout\": 30,\n }\n"
                            "*** Update File: tests/test_loader.py\n@@\n def test_retries():\n     assert load('retries') == 3\n+\n+\n+def test_timeout_default():\n+    assert load('timeout') == 30\n"
                            "*** End Patch"
                        )
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "add timeout setting",
                            "steps": [
                                {"description": "read config and loader", "status": "completed", "evidence": ["config.py"]},
                                {"description": "add timeout default", "status": "completed", "evidence": ["config.py"]},
                                {"description": "test it", "status": "completed", "evidence": ["tests/test_loader.py"]},
                            ],
                        }
                    },
                }
            ]
        },
        {"tool_calls": [{"name": "run_command", "arguments": {"command": [PY, "-m", "pytest", "-q"], "cwd": "."}}]},
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {
                        "summary": "timeout setting added through config + tests",
                        "claims": [{"statement": "timeout default is 30 and tests pass", "evidence_refs": ["config.py", "tests/test_loader.py", "L3 tests"]}],
                    },
                }
            ]
        },
    ]
    out = run_scripted(repo, tmp / "home", "add timeout setting", script)
    if out.session.state.status != Status.COMPLETED.value:
        return False, f"status={out.session.state.status}"
    if '"timeout": 30' not in (repo / "config.py").read_text(encoding="utf-8"):
        return False, "cross-file change missing"
    return True, "multi-file atomic patch verified by tests"


def eval_e4_new_file(tmp: Path) -> tuple[bool, str]:
    repo = make_fixture(tmp / "repo", "docs")
    script = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "app.py"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "add a version helper module",
                            "steps": [{"description": "write module and test", "status": "in_progress"}],
                        }
                    },
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "write_file",
                    "arguments": {"path": "version.py", "content": "VERSION = '1.2.3'\n\n\ndef get_version():\n    return VERSION\n", "create_only": True},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "write_file",
                    "arguments": {"path": "test_version.py", "content": "from version import get_version\n\n\ndef test_version():\n    assert get_version() == '1.2.3'\n", "create_only": True},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {
                        "plan": {
                            "objective": "add a version helper module",
                            "steps": [{"description": "write module and test", "status": "completed", "evidence": ["version.py", "test_version.py"]}],
                        }
                    },
                }
            ]
        },
        {"tool_calls": [{"name": "run_command", "arguments": {"command": [PY, "-m", "pytest", "-q", "test_version.py"], "cwd": "."}}]},
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {
                        "summary": "version helper added",
                        "claims": [{"statement": "get_version() returns 1.2.3 and its test passes", "evidence_refs": ["version.py", "test_version.py"]}],
                    },
                }
            ]
        },
    ]
    out = run_scripted(repo, tmp / "home", "add version helper", script)
    if out.session.state.status != Status.COMPLETED.value:
        return False, f"status={out.session.state.status}"
    if not (repo / "version.py").exists():
        return False, "new file missing"
    return True, "new file created and verified"


def eval_e5_recovery(tmp: Path) -> tuple[bool, str]:
    return probe_t5_recovery(tmp)


def eval_e6_permission(tmp: Path) -> tuple[bool, str]:
    return probe_t4_path_escape(tmp)


def eval_e7_false_success(tmp: Path) -> tuple[bool, str]:
    return probe_t4_false_success(tmp)


def eval_e8_loop(tmp: Path) -> tuple[bool, str]:
    return probe_t4_loop(tmp)


# --------------------------------------------------------------------------- suite


@dataclass
class Probe:
    name: str
    title: str
    fn: Callable[[Path], tuple[bool, str]]


HCS_PROBES = [
    Probe("P1", "adaptive agent loop (T1)", probe_t1_loop),
    Probe("P2", "tool interface changes the environment (T2)", probe_t2_environment),
    Probe("P3", "content-aware context + compaction (T3)", probe_t3_context),
    Probe("P3b", "no silent truncation (T3)", probe_t3_truncation),
    Probe("P4a", "false success rejected (T4)", probe_t4_false_success),
    Probe("P4b", "path escape blocked (T4)", probe_t4_path_escape),
    Probe("P4c", "loop detection (T4)", probe_t4_loop),
    Probe("P4d", "fail-closed approval (T4)", probe_t4_approval),
    Probe("P5", "crash recovery (T4)", probe_t5_recovery),
    Probe("P6", "claims require evidence (T4)", probe_t6_evidence),
]

E_PROBES = [
    Probe("E1", "read-only analysis", eval_e1_readonly),
    Probe("E2", "small bug fix", eval_e2_bugfix),
    Probe("E3", "cross-file change", eval_e3_cross_file),
    Probe("E4", "new file + test", eval_e4_new_file),
    Probe("E5", "crash recovery", eval_e5_recovery),
    Probe("E6", "permission control", eval_e6_permission),
    Probe("E7", "false-success protection", eval_e7_false_success),
    Probe("E8", "loop control", eval_e8_loop),
]


def run_probe(probe: Probe, workspace: Path | None = None) -> tuple[bool, str]:
    base = Path(workspace) if workspace else Path(tempfile.mkdtemp(prefix="jinlin-eval-"))
    base.mkdir(parents=True, exist_ok=True)
    try:
        return probe.fn(base)
    except Exception as exc:  # a probe must never take the suite down
        return False, f"probe crashed: {type(exc).__name__}: {exc}"
    finally:
        if workspace is None:
            shutil.rmtree(base, ignore_errors=True)


def run_suite(suite: str, workspace: Path | None = None, verbose: bool = True) -> int:
    probes: list[Probe] = []
    if suite in ("all", "hcs"):
        probes += HCS_PROBES
    if suite in ("all", "e1e8"):
        probes += E_PROBES
    passed = 0
    failed: list[Probe] = []
    for probe in probes:
        ok, detail = run_probe(probe, workspace)
        if ok:
            passed += 1
        else:
            failed.append(probe)
        if verbose:
            print(f"{'PASS' if ok else 'FAIL'}  {probe.name:<4} {probe.title}: {detail}")
    print(f"\n{passed}/{len(probes)} probes passed")
    if failed:
        print("failed: " + ", ".join(f"{p.name}({p.title})" for p in failed))
        return 1
    return 0
