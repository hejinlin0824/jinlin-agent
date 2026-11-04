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


def http_json(token, url, payload=None, method=None, headers=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"))
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body)
            except json.JSONDecodeError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def wait_for_status(ledger, worker_id, status, timeout=240.0):
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
        status, data = http_json(server.token, f"{server.base_url}/api/tasks")
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
        status, data = http_json(server.token, f"{server.base_url}/api/tasks/{spec.task_id}")
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
        status, body = http_json(server.token, f"{server.base_url}/")
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
        status, data = http_json(server.token, f"{server.base_url}/api/tasks/{spec.task_id}/cancel", payload={"reason": "owner"}, method="POST")
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
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and ledger.get_worker(worker_id)["status"] != "waiting_user":
            time.sleep(0.05)
        status, data = http_json(server.token, f"{server.base_url}/api/tasks/{spec.task_id}/answer", payload={"answer": "use X"}, method="POST")
        assert status == 200, data
        reached = wait_for_status(ledger, worker_id, "completed")
        row = ledger.get_worker(worker_id)
        # diagnostic on failure: the worker row carries the crash/failure reason
        assert reached and row["status"] == "completed", (
            f"worker stuck at {row['status']}; error={row.get('error')!r}"
        )
        # the worker row advances slightly before the task row; poll the task
        deadline = time.monotonic() + 60
        task_status = None
        while time.monotonic() < deadline:
            task_status = ledger.get_task(spec.task_id)["status"]
            if task_status == "completed":
                break
            time.sleep(0.1)
        assert task_status == "completed", f"task stuck at {task_status}"
    finally:
        server.shutdown()
        runtime.shutdown()
        ledger.close()


def test_control_endpoints_require_a_runtime(tmp_path):
    server, cfg = make_server(tmp_path)  # ledger-only mode
    ledger = TaskLedger(cfg).open()
    try:
        spec = make_task(ledger)
        status, _data = http_json(server.token, f"{server.base_url}/api/tasks/{spec.task_id}/cancel", payload={}, method="POST")
        assert status == 405
    finally:
        server.shutdown()
        ledger.close()


def test_unknown_task_is_404(tmp_path):
    server, _cfg = make_server(tmp_path)
    try:
        status, _data = http_json(server.token, f"{server.base_url}/api/tasks/task_nope")
        assert status == 404
    finally:
        server.shutdown()


# --------------------------------------------------------------------------- WP-R3 security


def test_index_html_never_embeds_untrusted_data(tmp_path):
    server, cfg = make_server(tmp_path)
    ledger = TaskLedger(cfg).open()
    try:
        hostile = TaskSpec(
            task_id=new_task_id(),
            root_task_id=None,
            title='<img src=x onerror="alert(1)">',
        )
        hostile.root_task_id = hostile.task_id
        ledger.create_task(hostile)
        status, body = http_json(server.token, f"{server.base_url}/")
        assert status == 200
        # the served page is a static template: no task data is interpolated
        assert "onerror" not in body
        assert "<img" not in body.replace("<!doctype", "")
        assert 'name="workbench-token"' in body  # token bootstrap for the page itself
        assert "innerHTML" not in body  # rendered exclusively via textContent
    finally:
        server.shutdown()
        ledger.close()


def test_post_requires_the_bearer_token(tmp_path):
    server, cfg = make_server(tmp_path)
    ledger = TaskLedger(cfg).open()
    try:
        spec = make_task(ledger)
        status, _ = http_json(None, f"{server.base_url}/api/tasks/{spec.task_id}/cancel", payload={}, method="POST")
        assert status == 401  # missing token
        status, _ = http_json(
            None,
            f"{server.base_url}/api/tasks/{spec.task_id}/cancel",
            payload={},
            method="POST",
            headers={"Authorization": "Bearer wrong"},
        )
        assert status == 401  # wrong token
        status, _ = http_json(None, f"{server.base_url}/api/tasks")
        assert status == 401  # /api reads are gated too
    finally:
        server.shutdown()
        ledger.close()


def test_foreign_origin_is_rejected_for_state_changes(tmp_path):
    server, cfg = make_server(tmp_path)
    ledger = TaskLedger(cfg).open()
    try:
        spec = make_task(ledger)
        status, _ = http_json(
            server.token,
            f"{server.base_url}/api/tasks/{spec.task_id}/cancel",
            payload={},
            method="POST",
            headers={"Origin": "http://evil.example"},
        )
        assert status == 403  # cross-site state change blocked
    finally:
        server.shutdown()
        ledger.close()


def test_host_header_must_be_loopback(tmp_path):
    import http.client

    server, cfg = make_server(tmp_path)
    ledger = TaskLedger(cfg).open()
    try:
        spec = make_task(ledger)
        port = server.base_url.rsplit(":", 1)[1]
        conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=10)
        conn.request("GET", "/api/tasks", headers={"Host": "evil.example", "Authorization": f"Bearer {server.token}"})
        resp = conn.getresponse()
        assert resp.status == 403  # DNS-rebinding style Host is rejected
        conn.close()
    finally:
        server.shutdown()
        ledger.close()


# --------------------------------------------------------------------------- WP-R9: task creation + decisions view


def _make_server_with_supervisor(tmp_path, worker_scripts, supervisor_script):
    from jinlin_agent.ledger import TaskLedger
    from jinlin_agent.model.mock import MockProvider
    from jinlin_agent.supervisor import Supervisor
    from jinlin_agent.worker_runtime import WorkerRuntime
    from test_worker_runtime import make_task as _unused  # noqa: F401

    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    worker_iter = iter(worker_scripts)
    runtime = WorkerRuntime(
        cfg, ledger,
        provider_factory=lambda: MockProvider(next(worker_iter)),
        sandbox_factory=lambda w: NullSandbox(str(w)),
        log=lambda m: None,
    )
    supervisor = Supervisor(cfg, ledger, runtime, provider=MockProvider(supervisor_script), log=lambda m: None)
    server = WorkbenchServer(cfg, runtime=runtime, supervisor=supervisor, port=0, ledger=ledger)
    server.start()
    return server, ledger, runtime, ws_dir(tmp_path)


def ws_dir(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text("# demo\n\nEntry point: app.main()\n", encoding="utf-8")
    return ws


def test_create_task_endpoint_drives_the_supervisor(tmp_path):
    plan = {"text": "```json\n" + json.dumps({"subtasks": [
        {"title": "analyze", "description": "analyze", "depends_on": [], "role": "worker",
         "capabilities": ["READ"], "acceptance_criteria": ["done"], "verification": "analysis"}
    ]}) + "\n```"}
    assess = {"text": json.dumps({"strategy": "plan_then_execute", "reasons": ["scripted"]})}
    finish = [
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {"tool_calls": [{"name": "update_plan", "arguments": {"plan": {"objective": "analyze", "steps": [{"description": "read", "status": "completed", "evidence": ["README.md"]}]}}}]},
        {"tool_calls": [{"name": "propose_completion", "arguments": {"summary": "read-only analysis finished", "claims": [{"statement": "entry is app.main", "evidence_refs": ["README.md"]}]}}]},
    ]
    from jinlin_agent.model.mock import MockProvider
    from jinlin_agent.supervisor import Supervisor
    from jinlin_agent.worker_runtime import WorkerRuntime

    cfg = load_config(home=tmp_path / "home")
    ledger = TaskLedger(cfg).open()
    worker_iter = iter([finish])
    runtime = WorkerRuntime(cfg, ledger, provider_factory=lambda: MockProvider(next(worker_iter)), sandbox_factory=lambda w: NullSandbox(str(w)), log=lambda m: None)
    supervisor = Supervisor(cfg, ledger, runtime, provider=MockProvider([assess, plan]), log=lambda m: None)
    server = WorkbenchServer(cfg, runtime=runtime, supervisor=supervisor, port=0, ledger=ledger)
    server.start()
    ws = ws_dir(tmp_path)
    try:
        status, data = http_json(
            server.token,
            f"{server.base_url}/api/tasks",
            payload={"title": "analyze the demo", "workspace": str(ws)},
            method="POST",
        )
        assert status == 201, data
        task_id = data["task_id"]
        deadline = time.monotonic() + 60
        row = None
        while time.monotonic() < deadline:
            row = ledger.get_task(task_id)
            if row and row["status"] in ("completed", "failed", "blocked"):
                break
            time.sleep(0.1)
        assert row["status"] == "completed", row
    finally:
        server.shutdown()
        runtime.shutdown()
        ledger.close()


def test_create_task_requires_supervisor(tmp_path):
    server, cfg = make_server(tmp_path)
    ledger = TaskLedger(cfg).open()
    try:
        status, _ = http_json(
            server.token,
            f"{server.base_url}/api/tasks",
            payload={"title": "x"},
            method="POST",
        )
        assert status == 405  # read-only workbench
    finally:
        server.shutdown()
        ledger.close()


def test_detail_includes_decisions_timeline(tmp_path):
    server, cfg = make_server(tmp_path)
    ledger = TaskLedger(cfg).open()
    try:
        spec = make_task(ledger)
        ledger.set_task_status(spec.task_id, "queued")
        ledger.emit(spec.task_id, "task.strategy_selected", {"root_task_id": spec.task_id, "strategy": "direct", "reasons": ["x"]})
        status, data = http_json(server.token, f"{server.base_url}/api/tasks/{spec.task_id}")
        assert status == 200
        assert any(d["type"] == "task.strategy_selected" for d in data["decisions"])
    finally:
        server.shutdown()
        ledger.close()
