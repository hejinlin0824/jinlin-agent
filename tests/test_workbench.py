"""WP-V2.0-9 regression: task workbench (observability + minimal control).

Read-only endpoints over the task ledger; the ONLY writes are task cancel and
answer (resume a waiting worker), and only when a live WorkerRuntime is
attached. Bound to 127.0.0.1. HTTP approvals are deliberately NOT exposed:
approvals are fail-closed interactive (v2 §9.6) and an unauthenticated HTTP
approval endpoint would be a bypass.
"""

import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from jinlin_agent.config import load_config
from jinlin_agent.ledger import TaskLedger
from jinlin_agent.model.mock import MockProvider
from jinlin_agent.sandbox import NullSandbox
from jinlin_agent.taskmodel import Budget, TaskSpec, new_task_id
from jinlin_agent.worker_runtime import WorkerRuntime
from jinlin_agent.workbench import WorkbenchServer

from test_worker_runtime import make_task, make_ws, read_only_script


def make_server(tmp_path, runtime=None):
    cfg = load_config(home=tmp_path / "home")
    server = WorkbenchServer(cfg, runtime=runtime, port=0)
    server.start()
    return server, cfg


def http_json(url, payload=None, method=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_for_status(ledger, worker_id, status, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        row = ledger.get_worker(worker_id)
        if row is not None and row["status"] == status:
            return True
        time.sleep(0.05)
    return False


def test_tasks_endpoint_reflects_the_ledger(tmp_path):
    server, cfg = make_server(tmp_path)
    ledger = TaskLedger(cfg).open()
    try:
        spec = make_task(ledger)
        ledger.set_task_status(spec.task_id, "queued")
        status, data = http_json(f"{server.base_url}/api/tasks")
        assert status == 200
        assert data["summary"]["total"] >= 1
        row = next(t for t in data["tasks"] if t["task_id"] == spec.task_id)
        assert row["status"] == "queued"
        assert row["title"] == spec.title
    finally:
        server.shutdown()
        ledger.close()


def test_task_detail_includes_workers_messages_and_events(tmp_path):
    from jinlin_agent.taskmodel import TaskMessage, new_message_id

    server, cfg = make_server(tmp_path)
    ledger = TaskLedger(cfg).open()
    try:
        spec = make_task(ledger)
        ledger.set_task_status(spec.task_id, "running", reason="demo")
        ledger.record_message(
            TaskMessage(
                message_id=new_message_id(),
                root_task_id=spec.task_id,
                task_id=spec.task_id,
                message_type="task.progress",
                payload={"note": "halfway"},
            )
        )
        status, data = http_json(f"{server.base_url}/api/tasks/{spec.task_id}")
        assert status == 200
        assert data["task"]["task_id"] == spec.task_id
        assert isinstance(data["workers"], list)
        assert any(m["message_type"] == "task.progress" for m in data["messages"])
        assert isinstance(data["events"], list) and data["events"]
    finally:
        server.shutdown()
        ledger.close()


def test_index_page_is_html(tmp_path):
    server, _cfg = make_server(tmp_path)
    try:
        status, body = http_json(f"{server.base_url}/")
        assert status == 200
        assert "jinlin" in body.lower()
    finally:
        server.shutdown()


def test_cancel_endpoint_stops_a_running_worker(tmp_path):
    ledger = None
    hold = threading.Event()

    def blocking_step(messages):
        hold.wait(timeout=30)
        return {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]}

    script = [blocking_step] + read_only_script()[1:]
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    worker_iter = iter([script])
    runtime = WorkerRuntime(
        cfg,
        ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda w: NullSandbox(str(w)),
        log=lambda msg: None,
    )
    server, _cfg = make_server(tmp_path, runtime=runtime)
    spec = make_task(ledger)
    try:
        worker_id = runtime.submit_task(spec.task_id, make_ws(tmp_path / "ws"))
        assert wait_for_status(ledger, worker_id, "running")
        status, data = http_json(f"{server.base_url}/api/tasks/{spec.task_id}/cancel", payload={"reason": "owner"}, method="POST")
        assert status == 200, data
        hold.set()
        assert wait_for_status(ledger, worker_id, "cancelled")
    finally:
        server.shutdown()
        runtime.shutdown()
        if ledger is not None:
            ledger.close()


def test_answer_endpoint_resumes_a_waiting_worker(tmp_path):
    ask_script = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {"plan": {"objective": "wait", "steps": [{"description": "ask", "status": "in_progress"}]}},
                }
            ]
        },
        {"tool_calls": [{"name": "request_user_input", "arguments": {"question": "which?"}}]},
    ]
    finish = [
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {"plan": {"objective": "wait", "steps": [{"description": "ask", "status": "completed", "evidence": ["README.md"]}]}},
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
    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    worker_iter = iter([ask_script, finish])
    runtime = WorkerRuntime(
        cfg,
        ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda w: NullSandbox(str(w)),
        log=lambda msg: None,
    )
    server, _cfg = make_server(tmp_path, runtime=runtime)
    spec = make_task(ledger)
    try:
        worker_id = runtime.submit_task(spec.task_id, make_ws(tmp_path / "ws"))
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and ledger.get_worker(worker_id)["status"] != "waiting_user":
            time.sleep(0.05)
        status, data = http_json(f"{server.base_url}/api/tasks/{spec.task_id}/answer", payload={"answer": "use X"}, method="POST")
        assert status == 200, data
        assert wait_for_status(ledger, worker_id, "completed")
        assert ledger.get_task(spec.task_id)["status"] == "completed"
    finally:
        server.shutdown()
        runtime.shutdown()
        ledger.close()


def test_control_endpoints_require_a_runtime(tmp_path):
    server, cfg = make_server(tmp_path)  # ledger-only mode
    ledger = TaskLedger(cfg).open()
    try:
        spec = make_task(ledger)
        status, _data = http_json(f"{server.base_url}/api/tasks/{spec.task_id}/cancel", payload={}, method="POST")
        assert status == 405
    finally:
        server.shutdown()
        ledger.close()


def test_unknown_task_is_404(tmp_path):
    server, _cfg = make_server(tmp_path)
    try:
        status, _data = http_json(f"{server.base_url}/api/tasks/task_nope")
        assert status == 404
    finally:
        server.shutdown()
