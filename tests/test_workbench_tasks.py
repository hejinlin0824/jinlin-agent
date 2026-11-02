from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from jinlin_agent.errors import AgentError
from jinlin_agent.web import create_server
from jinlin_agent.workbench import WorkbenchTaskManager


class _FakeService:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def run(self, task, *, read_only, auto_approve, approval_handler, on_session):
        on_session(SimpleNamespace(id="sess_20261102_120000_abcdef"), None)
        request = SimpleNamespace(
            approval_id="approval-1",
            tool="shell",
            risk="medium",
            summary="run pytest",
            reason="verification command",
            impact=["Runs the project's test suite"],
            arguments={"command": ["pytest"], "api_key": "must-not-leak"},
        )
        self.entered.set()
        decision, _note = approval_handler(request)
        self.release.set()
        return None, "completed" if decision != "denied" else "failed"


def _wait_for(manager, task_id, status, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = manager.get(task_id)
        if result["status"] == status:
            return result
        time.sleep(0.01)
    raise AssertionError(f"task did not reach {status}: {manager.get(task_id)}")


def test_task_manager_requires_matching_explicit_approval_and_hides_arguments():
    service = _FakeService()
    manager = WorkbenchTaskManager(service)
    created = manager.submit("run the tests", read_only=False)
    assert created["status"] in ("queued", "running")
    service.entered.wait(2)
    waiting = _wait_for(manager, created["task_id"], "waiting_approval")
    assert waiting["session_id"] == "sess_20261102_120000_abcdef"
    assert waiting["pending_approval"]["approval_id"] == "approval-1"
    assert waiting["pending_approval"]["summary"] == "run pytest"
    assert "arguments" not in waiting["pending_approval"]
    assert "api_key" not in json.dumps(waiting)

    with pytest.raises(AgentError) as exc:
        manager.approve(created["task_id"], "wrong-id", "approved_once")
    assert exc.value.code == "APPROVAL_STALE"

    result = manager.approve(created["task_id"], "approval-1", "approved_once")
    assert result["status"] == "running"
    service.release.wait(2)
    _wait_for(manager, created["task_id"], "completed")


def test_task_manager_rejects_concurrent_tasks_and_invalid_approval():
    service = _FakeService()
    manager = WorkbenchTaskManager(service)
    first = manager.submit("first task", read_only=True)
    service.entered.wait(2)
    _wait_for(manager, first["task_id"], "waiting_approval")

    with pytest.raises(AgentError) as exc:
        manager.submit("second task")
    assert exc.value.code == "TASK_BUSY"

    with pytest.raises(AgentError) as exc:
        manager.approve(first["task_id"], "approval-1", "approve_everything")
    assert exc.value.code == "REQUEST_INVALID"

    manager.approve(first["task_id"], "approval-1", "denied")
    service.release.wait(2)
    _wait_for(manager, first["task_id"], "failed")


def _http(url, token=None, method="GET", body=None, headers=None):
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_task_http_routes_require_auth_and_validate_origin_and_body(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    server, _service, token = create_server(root, port=0, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        status, payload = _http(base + "/api/v1/tasks", method="POST", body={"task": "hello"})
        assert status == 401 and payload["error"]["code"] == "UNAUTHORIZED"

        status, payload = _http(
            base + "/api/v1/tasks", token, method="POST", body={"task": "hello"},
            headers={"Origin": "http://evil.example"},
        )
        assert status == 403 and payload["error"]["code"] == "ORIGIN_REJECTED"

        status, payload = _http(
            base + "/api/v1/tasks", token, method="POST", body={"task": "hello", "auto_approve": True},
        )
        assert status == 400 and payload["error"]["code"] == "REQUEST_INVALID"

        status, payload = _http(base + "/api/v1/tasks", token)
        assert status == 200 and payload["data"] == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_task_http_submit_is_accepted_but_never_auto_approves(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    server, _service, token = create_server(root, port=0, token="test-token")

    def fake_run(task, *, read_only, auto_approve, approval_handler, on_session):
        assert read_only is True
        assert auto_approve is False
        on_session(SimpleNamespace(id="sess_20261102_120000_abcdef"), None)
        return None, "completed"

    server.task_manager.service.run = fake_run
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        status, payload = _http(
            base + "/api/v1/tasks", token, method="POST",
            body={"task": "inspect project", "read_only": True},
        )
        assert status == 202
        task_id = payload["data"]["task_id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, payload = _http(base + "/api/v1/tasks/" + task_id, token)
            if payload["data"]["status"] == "completed":
                break
            time.sleep(0.01)
        assert status == 200
        assert payload["data"]["session_id"] == "sess_20261102_120000_abcdef"
        assert payload["data"]["status"] == "completed"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
