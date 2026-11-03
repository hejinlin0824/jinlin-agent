"""WP-V2.0-4 regression: task protocol pump, envelope validation, reviewer flow.

ADR-004: the task protocol (task.* / review.* messages in the task event
stream) is separate from Git collaboration; message_id is the idempotency key;
delivery is at-least-once, so processing must be exactly-once per message and
handlers must tolerate a crash between handler and mark. Schema v1->v2
migration adds messages.processed_at and tasks.requires_review (ADR-008).
"""

import json
import sqlite3
from pathlib import Path

import pytest

from jinlin_agent.config import load_config
from jinlin_agent.errors import TASK_MESSAGE_INVALID, AgentError
from jinlin_agent.ledger import TaskLedger
from jinlin_agent.model.mock import MockProvider
from jinlin_agent.protocol import (
    MessagePump,
    ReviewResult,
    deliver_result,
    conduct_review,
    validate_envelope,
    validate_review_result,
)
from jinlin_agent.taskmodel import Budget, TaskMessage, TaskSpec, new_message_id, new_task_id


# --------------------------------------------------------------------------- helpers


def make_ledger(tmp_path):
    cfg = load_config(home=tmp_path / "home")
    return TaskLedger(cfg).open(), cfg


def make_task(ledger: TaskLedger, requires_review: bool = False) -> TaskSpec:
    tid = new_task_id()
    spec = TaskSpec(
        task_id=tid,
        root_task_id=tid,
        title="protocol demo",
        capabilities=["READ"],
        budget=Budget(max_tokens=300_000, max_tool_calls=200, timeout_seconds=3600, max_attempts=1),
        requires_review=requires_review,
    )
    ledger.create_task(spec)
    return spec


def envelope(message_type: str = "task.progress", **overrides) -> TaskMessage:
    base = dict(
        message_id=new_message_id(),
        root_task_id="task_root",
        task_id="task_root",
        message_type=message_type,
        payload={"note": "x"},
    )
    base.update(overrides)
    return TaskMessage(**base)


# --------------------------------------------------------------------------- schema migration


def test_schema_v1_migrates_to_v2(tmp_path):
    ledger, cfg = make_ledger(tmp_path)
    spec = make_task(ledger)
    ledger.close()

    # rewind the projection to the exact v1 shape (ALTER DROP keeps the PK)
    conn = sqlite3.connect(str(cfg.ledger_path))
    conn.execute("ALTER TABLE messages DROP COLUMN processed_at")
    conn.execute("ALTER TABLE tasks DROP COLUMN requires_review")
    conn.execute("PRAGMA user_version = 1")
    conn.commit()
    conn.close()

    ledger2 = TaskLedger(cfg).open()
    row = ledger2.get_task(spec.task_id)
    assert row is not None
    assert row["requires_review"] is False  # v1 default
    pump = MessagePump(ledger2)
    pump.publish(envelope(root_task_id=spec.task_id, task_id=spec.task_id))
    seen = pump.drain(spec.task_id, lambda msg: None)
    assert seen == 1
    ledger2.close()


def test_newer_schema_still_fails_closed(tmp_path):
    ledger, cfg = make_ledger(tmp_path)
    ledger.close()
    conn = sqlite3.connect(str(cfg.ledger_path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(AgentError):
        TaskLedger(cfg).open()


# --------------------------------------------------------------------------- envelope validation


def test_envelope_validation_rules():
    good = envelope()
    assert validate_envelope(good) == []
    problems = validate_envelope(envelope(message_type=""))
    assert any("message_type" in p for p in problems)
    problems = validate_envelope(envelope(message_id=""))
    assert any("message_id" in p for p in problems)
    orphan = envelope(root_task_id="", task_id="")
    assert any("root_task_id" in p or "task_id" in p for p in validate_envelope(orphan))


def test_protocol_major_version_gate():
    assert validate_envelope(envelope(protocol_version="1.0")) == []
    assert validate_envelope(envelope(protocol_version="1.9")) == []  # minor versions tolerated
    problems = validate_envelope(envelope(protocol_version="2.0"))
    assert any("protocol_version" in p for p in problems)


def test_drain_rejects_incompatible_version(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    spec = make_task(ledger)
    pump = MessagePump(ledger)
    pump.publish(envelope(root_task_id=spec.task_id, task_id=spec.task_id, protocol_version="2.0"))
    with pytest.raises(AgentError) as exc:
        pump.drain(spec.task_id, lambda msg: None)
    assert exc.value.code == TASK_MESSAGE_INVALID
    ledger.close()


# --------------------------------------------------------------------------- pump semantics


def test_drain_is_exactly_once_per_message(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    spec = make_task(ledger)
    pump = MessagePump(ledger)
    for _ in range(3):
        pump.publish(envelope(root_task_id=spec.task_id, task_id=spec.task_id))
    seen: list[str] = []
    assert pump.drain(spec.task_id, lambda msg: seen.append(msg.message_id)) == 3
    assert len(seen) == 3
    assert pump.drain(spec.task_id, lambda msg: seen.append(msg.message_id)) == 0  # no redelivery
    assert len(seen) == 3
    ledger.close()


def test_drain_filters_by_message_type(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    spec = make_task(ledger)
    pump = MessagePump(ledger)
    pump.publish(envelope(root_task_id=spec.task_id, task_id=spec.task_id, message_type="task.progress"))
    pump.publish(envelope(root_task_id=spec.task_id, task_id=spec.task_id, message_type="task.result"))
    seen: list[str] = []
    assert pump.drain(spec.task_id, lambda msg: seen.append(msg.message_type), message_type="task.result") == 1
    assert seen == ["task.result"]
    # the progress message is still pending for its own consumer
    assert pump.drain(spec.task_id, lambda msg: seen.append(msg.message_type), message_type="task.progress") == 1
    ledger.close()


def test_out_of_order_messages_process_independently(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    spec = make_task(ledger)
    pump = MessagePump(ledger)
    child = envelope(root_task_id=spec.task_id, task_id=spec.task_id, correlation_id="msg_parent_missing")
    parent = envelope(root_task_id=spec.task_id, task_id=spec.task_id)
    pump.publish(child)
    pump.publish(parent)
    processed = pump.drain(spec.task_id, lambda msg: None)
    assert processed == 2  # a missing correlation target never blocks the pump
    ledger.close()


# --------------------------------------------------------------------------- delivery protocol


def test_deliver_result_is_idempotent(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    spec = make_task(ledger)
    ledger.set_task_status(spec.task_id, "running")
    result = {
        "status": "completed",
        "summary": "did the work",
        "changed_files": ["a.py"],
        "tests": [{"command": "pytest -q", "status": "passed", "exit_code": 0}],
    }
    first = deliver_result(ledger, spec, worker_id="worker_1", attempt=1, result=result)
    second = deliver_result(ledger, spec, worker_id="worker_1", attempt=1, result=result)
    assert first is True and second is False  # duplicate delivery: no second side effect
    row = ledger.get_task(spec.task_id)
    assert row["result"]["summary"] == "did the work"
    assert len(ledger.list_messages(spec.task_id)) == 1
    ledger.close()


def test_deliver_result_validates_payload(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    spec = make_task(ledger)
    with pytest.raises(AgentError):
        deliver_result(ledger, spec, worker_id="worker_1", attempt=1, result={"status": "completed"})
    ledger.close()


# --------------------------------------------------------------------------- reviewer protocol


def test_review_result_validation():
    assert validate_review_result({"status": "accepted", "findings": [], "required_actions": []}) == []
    assert validate_review_result({"status": "rejected", "findings": [], "required_actions": []})
    ok = validate_review_result(
        {
            "status": "rejected",
            "findings": [{"severity": "high", "file": "a.py", "line": 1, "description": "null deref", "evidence": "test_x"}],
            "required_actions": ["handle null"],
        }
    )
    assert ok == []
    assert validate_review_result({"status": "maybe"})  # unknown verdict
    assert validate_review_result({"status": "rejected", "required_actions": ["fix"], "findings": [{"severity": "fatal"}]})


def test_conduct_review_parses_scripted_verdict(tmp_path):
    accepted = conduct_review(
        MockProvider([{"text": "```json\n" + json.dumps({"status": "accepted", "findings": [], "required_actions": []}) + "\n```"}]),
        task_title="demo",
        result={"summary": "did it", "changed_files": ["a.py"], "tests": [{"command": "pytest -q", "status": "passed"}]},
        evidence_summary="pytest -q passed",
    )
    assert accepted.status == "accepted"
    rejected = conduct_review(
        MockProvider([{"text": json.dumps({"status": "rejected", "findings": [{"severity": "high", "file": "a.py", "line": 3, "description": "bug", "evidence": "test"}], "required_actions": ["fix bug"]})}]),
        task_title="demo",
        result={"summary": "did it", "changed_files": [], "tests": []},
        evidence_summary="",
    )
    assert rejected.status == "rejected"
    assert rejected.required_actions == ["fix bug"]


def test_record_review_writes_protocol_message(tmp_path):
    ledger, _cfg = make_ledger(tmp_path)
    spec = make_task(ledger)
    review = ReviewResult(status="rejected", findings=[], required_actions=["add tests"])
    from jinlin_agent.protocol import record_review

    record_review(ledger, spec, worker_id="worker_1", review=review, correlation_id="msg_parent")
    messages = ledger.list_messages(spec.task_id)
    assert [m["message_type"] for m in messages] == ["review.rejected"]
    ledger.close()


# --------------------------------------------------------------------------- supervisor review gate


def _plan_json(subtasks: list[dict]) -> dict:
    return {"text": "```json\n" + json.dumps({"subtasks": subtasks}) + "\n```"}


def _sub(title: str, requires_review: bool = False, depends_on: list[str] | None = None) -> dict:
    return {
        "title": title,
        "description": f"do {title}",
        "depends_on": depends_on or [],
        "role": "worker",
        "capabilities": ["READ"],
        "acceptance_criteria": [f"{title} done"],
        "requires_review": requires_review,
    }


def _read_only_script():
    return [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {"plan": {"objective": "x", "steps": [{"description": "read", "status": "completed", "evidence": ["README.md"]}]}},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {"summary": "read-only analysis finished", "claims": [{"statement": "entry is app.main", "evidence_refs": ["README.md"]}]},
                }
            ]
        },
    ]


def _review_json(payload: dict) -> dict:
    return {"text": "```json\n" + json.dumps(payload) + "\n```"}


def _make_supervisor_stack(tmp_path, worker_scripts, supervisor_script):
    from jinlin_agent.worker_runtime import WorkerRuntime
    from jinlin_agent.supervisor import Supervisor
    from test_worker_runtime import make_ws

    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    worker_iter = iter(worker_scripts)
    runtime = WorkerRuntime(
        cfg,
        ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda ws: __import__("jinlin_agent.sandbox", fromlist=["NullSandbox"]).NullSandbox(str(ws)),
        log=lambda msg: None,
    )
    supervisor = Supervisor(cfg, ledger, runtime, provider=MockProvider(supervisor_script), log=lambda msg: None)
    return cfg, ledger, runtime, supervisor, make_ws(tmp_path / "ws")


def _root(ledger: TaskLedger, title: str) -> TaskSpec:
    from test_worker_runtime import make_task as _unused  # noqa: F401  (pattern parity)

    tid = new_task_id()
    spec = TaskSpec(task_id=tid, root_task_id=tid, title=title, capabilities=["READ"])
    ledger.create_task(spec)
    return spec


def test_supervisor_review_gate_accepts(tmp_path):
    cfg, ledger, runtime, supervisor, ws = _make_supervisor_stack(
        tmp_path,
        [_read_only_script()],
        supervisor_script=[
            _review_json({"strategy": "plan_then_execute", "reasons": ["scripted"]}),
            _plan_json([_sub("reviewed work", requires_review=True)]),
            _review_json({"status": "accepted", "findings": [], "required_actions": []}),
        ],
    )
    root = _root(ledger, "1) reviewed work")
    try:
        outcome = supervisor.execute_root(root, ws)
        assert outcome["status"] == "completed", outcome
        messages = [m["message_type"] for m in ledger.list_messages(root.task_id)]
        assert "review.requested" in messages and "review.accepted" in messages
        assert ledger.get_task(root.task_id)["status"] == "completed"
    finally:
        runtime.shutdown()
        ledger.close()


def test_supervisor_review_gate_rejection_triggers_replan(tmp_path):
    replacement = _plan_json([_sub("reviewed work (fixed)")])
    cfg, ledger, runtime, supervisor, ws = _make_supervisor_stack(
        tmp_path,
        [_read_only_script(), _read_only_script()],
        supervisor_script=[
            _review_json({"strategy": "plan_then_execute", "reasons": ["scripted"]}),
            _plan_json([_sub("reviewed work", requires_review=True)]),
            _review_json({"status": "rejected", "findings": [{"severity": "high", "file": "a.py", "line": 1, "description": "bug", "evidence": "t"}], "required_actions": ["fix the bug"]}),
            replacement,
            _review_json({"status": "accepted", "findings": [], "required_actions": []}),
        ],
    )
    root = _root(ledger, "1) reviewed work")
    try:
        outcome = supervisor.execute_root(root, ws)
        # the rejection fails the reviewed task (bounded replan replaces it);
        # the replacement carries no review requirement, so the root completes
        assert outcome["status"] == "completed", outcome
        assert by_title_failed(ledger, root.task_id, "reviewed work")
        messages = [m["message_type"] for m in ledger.list_messages(root.task_id)]
        assert "review.rejected" in messages
        assert [t for t in ledger.list_tasks(root.task_id) if t["title"] == "reviewed work (fixed)"][0]["status"] == "completed"
    finally:
        runtime.shutdown()
        ledger.close()


def by_title_failed(ledger: TaskLedger, root_id: str, title: str) -> bool:
    rows = [r for r in ledger.list_tasks(root_id) if r["title"] == title]
    return bool(rows) and rows[0]["status"] == "failed"
