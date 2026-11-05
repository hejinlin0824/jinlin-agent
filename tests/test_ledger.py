"""WP-V2.0-1 regression: task model + task ledger.

ADR-003: the session event log stays the execution truth; the sqlite ledger is
the task-layer projection. ADR-008: stdlib sqlite3, user_version fail-closed.
ADR-009: terminal task/worker states are never revived (new attempt instead).
"""

import json
import sqlite3

import pytest

from jinlin_agent.config import load_config
from jinlin_agent.domain import Status
from jinlin_agent.errors import LEDGER_UNAVAILABLE, TASK_ALREADY_EXISTS, TASK_NOT_FOUND, TASK_TRANSITION_INVALID, AgentError
from jinlin_agent.events import read_events
from jinlin_agent.ledger import TaskLedger
from jinlin_agent.state import SessionState, apply_event
from jinlin_agent.taskmodel import (
    SESSION_TO_TASK_STATUS,
    TASK_TERMINAL_STATUSES,
    Budget,
    TaskMessage,
    TaskSpec,
    TaskStatus,
    WorkerRecord,
    can_transition,
    new_message_id,
    new_task_id,
    new_worker_id,
    validate_dependency_graph,
    validate_task_result,
)


# --------------------------------------------------------------------------- helpers


def make_cfg(tmp_path):
    return load_config(home=tmp_path)


def make_ledger(tmp_path):
    return TaskLedger(make_cfg(tmp_path)).open()


def root_spec(title="root task"):
    tid = new_task_id()
    return TaskSpec(task_id=tid, root_task_id=tid, title=title)


def child_spec(root, title="child task", deps=None):
    return TaskSpec(
        task_id=new_task_id(),
        root_task_id=root.task_id,
        parent_task_id=root.task_id,
        title=title,
        dependencies=deps or [],
    )


# --------------------------------------------------------------------------- status machine


def test_terminal_task_statuses_are_frozen():
    assert TASK_TERMINAL_STATUSES == {"completed", "failed", "cancelled"}
    for terminal in TASK_TERMINAL_STATUSES:
        for nxt in ("queued", "running", "waiting_user", "completed", "failed", "cancelled"):
            assert not can_transition(terminal, nxt), f"{terminal} must not revive to {nxt}"


def test_happy_path_transitions():
    assert can_transition("created", "queued")
    assert can_transition("created", "running")  # simple root tasks skip the queue
    assert can_transition("queued", "running")
    assert can_transition("running", "completed")
    assert can_transition("running", "waiting_approval")
    assert can_transition("waiting_approval", "running")
    assert can_transition("running", "waiting_user")
    assert can_transition("waiting_user", "running")
    assert can_transition("running", "blocked")
    assert can_transition("blocked", "running")
    assert can_transition("blocked", "failed")


def test_illegal_transitions():
    assert not can_transition("created", "completed")  # nothing completes without running
    assert not can_transition("waiting_approval", "completed")
    assert not can_transition("queued", "completed")
    assert not can_transition("completed", "failed")


def test_review_pending_stage_transitions():
    """WP-V2.0-4: a requires_review task parks in review_pending after the
    worker finishes; only the review verdict completes or fails it."""
    assert can_transition("running", "review_pending")
    assert can_transition("review_pending", "completed")
    assert can_transition("review_pending", "failed")
    assert can_transition("review_pending", "cancelled")
    assert not can_transition("review_pending", "running")
    assert not can_transition("review_pending", "queued")
    assert not can_transition("completed", "review_pending")  # terminal never re-enters review


def test_session_to_task_mapping_covers_every_session_status():
    """ADR-003: the TaskStatus<->SessionStatus mapping is frozen and tested."""
    assert set(SESSION_TO_TASK_STATUS) == {s.value for s in Status}
    task_statuses = {t.value for t in TaskStatus}
    for src, dst in SESSION_TO_TASK_STATUS.items():
        assert dst in task_statuses, f"session status {src} maps to unknown task status {dst}"
    # the reserved session "paused" (never emitted today) holds its task blocked
    assert SESSION_TO_TASK_STATUS["paused"] == "blocked"


# --------------------------------------------------------------------------- validation


def test_dependency_graph_validation():
    a = TaskSpec(task_id="task_a", root_task_id="task_root", title="A", dependencies=["task_b"])
    b = TaskSpec(task_id="task_b", root_task_id="task_root", title="B", dependencies=["task_a"])
    errors = validate_dependency_graph([a, b])
    assert any("cycle" in e.lower() for e in errors)

    lonely = TaskSpec(task_id="task_c", root_task_id="task_root", title="C", dependencies=["task_missing"])
    assert any("task_missing" in e for e in validate_dependency_graph([lonely]))

    self_dep = TaskSpec(task_id="task_d", root_task_id="task_root", title="D", dependencies=["task_d"])
    assert any("itself" in e for e in validate_dependency_graph([self_dep]))

    chain_root = TaskSpec(task_id="task_r", root_task_id="task_r", title="R")
    c1 = TaskSpec(task_id="task_c1", root_task_id="task_r", parent_task_id="task_r", title="C1")
    c2 = TaskSpec(task_id="task_c2", root_task_id="task_r", parent_task_id="task_c1", title="C2", dependencies=["task_c1"])
    assert validate_dependency_graph([chain_root, c1, c2]) == []


def test_task_result_validation():
    assert validate_task_result({})  # no status at all
    assert validate_task_result({"status": "completed", "summary": "  "})  # empty summary
    assert validate_task_result({"status": "completed", "summary": "done", "tests": [{"command": "pytest -q"}]})
    ok = validate_task_result(
        {
            "status": "completed",
            "summary": "added validation",
            "changed_files": ["src/api.py", "tests/test_api.py"],
            "commit": "abc1234",
            "tests": [{"command": "python -m pytest -q", "status": "passed", "exit_code": 0}],
            "known_risks": [],
            "follow_up": [],
        }
    )
    assert ok == []


# --------------------------------------------------------------------------- ledger: tasks


def test_task_lifecycle_and_persistence(tmp_path):
    lg = make_ledger(tmp_path)
    cfg = make_cfg(tmp_path)
    root = root_spec()
    child = child_spec(root, deps=[])
    lg.create_task(root)
    lg.create_task(child)

    lg.set_task_status(root.task_id, "running", reason="direct execution")
    lg.set_task_status(child.task_id, "queued")
    lg.set_task_status(child.task_id, "running")
    lg.record_result(
        child.task_id,
        {"status": "completed", "summary": "did the work", "changed_files": ["x.py"]},
    )
    lg.set_task_status(child.task_id, "completed", reason="result recorded")
    lg.close()

    # a brand-new ledger instance (process restart) sees the same state
    lg2 = TaskLedger(cfg).open()
    assert lg2.get_task(root.task_id)["status"] == "running"
    child_row = lg2.get_task(child.task_id)
    assert child_row["status"] == "completed"
    assert child_row["result"]["summary"] == "did the work"
    assert child_row["root_task_id"] == root.task_id
    assert {r["task_id"] for r in lg2.list_tasks(root.task_id)} == {root.task_id, child.task_id}
    lg2.close()

    # the task event stream is in the tasks dir, attributed to the root task
    events = list(read_events(cfg.tasks_dir / root.task_id))
    types = [e["type"] for e in events]
    assert types[0] == "task.created"
    assert types.count("task.status_changed") == 4
    assert "task.result_recorded" in types
    assert all(e["session_id"] == root.task_id for e in events)


def test_illegal_transition_is_rejected_and_state_unchanged(tmp_path):
    lg = make_ledger(tmp_path)
    root = root_spec()
    lg.create_task(root)
    with pytest.raises(AgentError) as exc:
        lg.set_task_status(root.task_id, "completed")
    assert exc.value.code == TASK_TRANSITION_INVALID
    assert lg.get_task(root.task_id)["status"] == "created"
    with pytest.raises(AgentError) as exc:
        lg.set_task_status("task_nope", "running")
    assert exc.value.code == TASK_NOT_FOUND
    lg.close()


def test_duplicate_task_id_rejected(tmp_path):
    lg = make_ledger(tmp_path)
    spec = root_spec()
    lg.create_task(spec)
    with pytest.raises(AgentError) as exc:
        lg.create_task(spec)
    assert exc.value.code == TASK_ALREADY_EXISTS
    lg.close()


# --------------------------------------------------------------------------- ledger: workers


def test_worker_lifecycle_and_new_attempt(tmp_path):
    lg = make_ledger(tmp_path)
    root = root_spec()
    child = child_spec(root)
    lg.create_task(root)
    lg.create_task(child)

    w1 = WorkerRecord(worker_id=new_worker_id(), task_id=child.task_id, root_task_id=root.task_id, attempt=1, status="running")
    lg.register_worker(w1)
    lg.set_worker_status(w1.worker_id, "completed")
    # terminal workers are never revived; a retry is a new attempt/worker
    with pytest.raises(AgentError) as exc:
        lg.set_worker_status(w1.worker_id, "running")
    assert exc.value.code == TASK_TRANSITION_INVALID

    w2 = WorkerRecord(worker_id=new_worker_id(), task_id=child.task_id, root_task_id=root.task_id, attempt=2, status="running")
    lg.register_worker(w2)
    rows = lg.list_workers(child.task_id)
    assert [r["attempt"] for r in rows] == [1, 2]
    assert {r["status"] for r in rows} == {"completed", "running"}
    lg.close()


# --------------------------------------------------------------------------- ledger: messages


def test_message_id_is_idempotent(tmp_path):
    lg = make_ledger(tmp_path)
    root = root_spec()
    lg.create_task(root)
    msg = TaskMessage(
        message_id=new_message_id(),
        root_task_id=root.task_id,
        task_id=root.task_id,
        worker_id="worker_x",
        attempt=1,
        message_type="task.progress",
        payload={"note": "halfway"},
    )
    assert lg.record_message(msg) is True
    assert lg.record_message(msg) is False  # duplicate delivery: no second side effect
    assert len(lg.list_messages(root.task_id)) == 1

    events = [e for e in read_events(make_cfg(tmp_path).tasks_dir / root.task_id)]
    assert len([e for e in events if e["type"] == "task.message"]) == 1
    lg.close()


# --------------------------------------------------------------------------- rebuild / schema


def test_rebuild_from_task_event_logs(tmp_path):
    cfg = make_cfg(tmp_path)
    lg = TaskLedger(cfg).open()
    root = root_spec()
    child = child_spec(root)
    lg.create_task(root)
    lg.create_task(child)
    lg.set_task_status(root.task_id, "running")
    lg.set_task_status(child.task_id, "queued")
    lg.set_task_status(child.task_id, "running")
    lg.set_task_status(child.task_id, "failed", reason="worker crashed twice")
    w = WorkerRecord(worker_id=new_worker_id(), task_id=child.task_id, root_task_id=root.task_id, attempt=1, status="failed")
    lg.register_worker(w)
    lg.record_message(
        TaskMessage(
            message_id=new_message_id(),
            root_task_id=root.task_id,
            task_id=child.task_id,
            message_type="task.failed",
            payload={"reason": "crash"},
        )
    )
    lg.close()

    cfg.ledger_path.unlink()  # lose the projection entirely
    lg2 = TaskLedger(cfg).open()
    counts = lg2.rebuild()
    assert counts["tasks"] == 2
    assert counts["workers"] == 1
    assert counts["messages"] == 1
    assert lg2.get_task(child.task_id)["status"] == "failed"
    assert lg2.get_task(root.task_id)["status"] == "running"
    assert lg2.get_worker(w.worker_id)["status"] == "failed"
    assert len(lg2.list_messages(root.task_id)) == 1
    lg2.close()


def test_ledger_fails_closed_on_newer_schema(tmp_path):
    cfg = make_cfg(tmp_path)
    TaskLedger(cfg).open().close()
    conn = sqlite3.connect(str(cfg.ledger_path))
    conn.execute("PRAGMA user_version = 99")
    conn.commit()
    conn.close()
    with pytest.raises(AgentError) as exc:
        TaskLedger(cfg).open()
    assert exc.value.code == LEDGER_UNAVAILABLE


# --------------------------------------------------------------------------- session-state fold (schema 收编)


def test_report_written_folds_into_session_state():
    """loop.py emits report.written on the failure path; the fold used to ignore it."""
    s = SessionState()
    apply_event(s, {"type": "report.written", "payload": {"report_artifact_id": "artifact_abc"}})
    assert s.report_artifact_id == "artifact_abc"


# --------------------------------------------------------------------------- CLI


def test_cli_ledger_summary_and_rebuild(tmp_path, monkeypatch, capsys):
    from jinlin_agent.cli import main

    cfg = make_cfg(tmp_path)
    lg = TaskLedger(cfg).open()
    root = root_spec(title="the root of all evil")
    lg.create_task(root)
    lg.set_task_status(root.task_id, "running")
    lg.close()

    monkeypatch.setenv("JINLIN_HOME", str(tmp_path))
    assert main(["ledger", "--workspace", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "tasks: 1" in out
    assert "running=1" in out
    assert root.task_id in out
    assert "the root of all evil" in out

    cfg.ledger_path.unlink()
    assert main(["ledger", "--workspace", str(tmp_path), "--rebuild"]) == 0
    out = capsys.readouterr().out
    assert "rebuilt" in out
    assert "tasks: 1" in out


def test_worker_session_binding_survives_rebuild(tmp_path):
    """resume depends on the worker->session binding; it must live in the
    event stream (worker.session_bound), not only in the projection."""
    cfg = make_cfg(tmp_path)
    lg = TaskLedger(cfg).open()
    root = root_spec()
    lg.create_task(root)
    w = WorkerRecord(worker_id=new_worker_id(), task_id=root.task_id, root_task_id=root.task_id, attempt=1, status="running")
    lg.register_worker(w)
    lg.set_worker_session(w.worker_id, "sess_20261103_120000_abc123")
    assert lg.get_worker(w.worker_id)["session_id"] == "sess_20261103_120000_abc123"
    lg.close()

    cfg.ledger_path.unlink()
    lg2 = TaskLedger(cfg).open()
    lg2.rebuild()
    assert lg2.get_worker(w.worker_id)["session_id"] == "sess_20261103_120000_abc123"
    lg2.close()


# --------------------------------------------------------------------------- WP-C3: integration state semantics


def test_record_result_sets_pending_for_changed_files(tmp_path):
    """A deliverable WITH changes must be integration=pending at record time
    (live SQLite write, not only on replay)."""
    lg = make_ledger(tmp_path)
    root = root_spec()
    lg.create_task(root)
    lg.record_result(
        root.task_id,
        {"status": "completed", "summary": "did the work", "changed_files": ["x.py"]},
    )
    assert lg.get_task(root.task_id)["integration"] == "pending"
    lg.close()


def test_record_result_sets_not_needed_for_empty_deliverable(tmp_path):
    lg = make_ledger(tmp_path)
    root = root_spec()
    lg.create_task(root)
    lg.record_result(
        root.task_id,
        {"status": "completed", "summary": "read-only analysis", "changed_files": []},
    )
    assert lg.get_task(root.task_id)["integration"] == "not_needed"
    lg.close()


def test_rebuild_restores_pending_integration_state(tmp_path):
    cfg = make_cfg(tmp_path)
    lg = make_ledger(tmp_path)
    root = root_spec()
    lg.create_task(root)
    lg.record_result(
        root.task_id,
        {"status": "completed", "summary": "did the work", "changed_files": ["x.py"]},
    )
    lg.close()
    # delete the projection entirely: rebuild() must restore it from events
    import os

    os.remove(tmp_path / "ledger.db")
    lg2 = TaskLedger(cfg).open()
    counts = lg2.rebuild()
    assert counts.get("tasks", 0) >= 1 or any(isinstance(v, int) and v >= 1 for v in counts.values())
    assert lg2.get_task(root.task_id)["integration"] == "pending"
    lg2.close()


def test_rebuild_restores_not_needed_integration_state(tmp_path):
    cfg = make_cfg(tmp_path)
    lg = make_ledger(tmp_path)
    root = root_spec()
    lg.create_task(root)
    lg.record_result(
        root.task_id,
        {"status": "completed", "summary": "read-only", "changed_files": []},
    )
    lg.close()
    import os

    os.remove(tmp_path / "ledger.db")
    lg2 = TaskLedger(cfg).open()
    lg2.rebuild()
    assert lg2.get_task(root.task_id)["integration"] == "not_needed"
    lg2.close()


def test_integration_state_is_event_sourced_not_projection_only(tmp_path):
    """The result_recorded event in the append-only stream carries the result
    payload; the live UPDATE and the rebuild must land on the SAME state."""
    cfg = make_cfg(tmp_path)
    lg = make_ledger(tmp_path)
    root = root_spec()
    lg.create_task(root)
    lg.record_result(
        root.task_id,
        {"status": "completed", "summary": "did the work", "changed_files": ["x.py"]},
    )
    live_row = lg.get_task(root.task_id)
    events = list(read_events(cfg.tasks_dir / root.task_id))
    recorded = [e for e in events if e["type"] == "task.result_recorded"]
    assert recorded and recorded[-1]["payload"]["result"]["changed_files"] == ["x.py"]
    lg.close()

    import os

    os.remove(tmp_path / "ledger.db")
    lg2 = TaskLedger(cfg).open()
    lg2.rebuild()
    replayed = lg2.get_task(root.task_id)
    assert replayed["integration"] == live_row["integration"] == "pending"
    assert replayed["result"]["summary"] == live_row["result"]["summary"]
    lg2.close()
