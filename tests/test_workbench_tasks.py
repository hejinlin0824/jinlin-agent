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

    def run(self, task, *, read_only, auto_approve, approval_handler, on_session, stream_callback=None):
        on_session(SimpleNamespace(id="sess_20261102_120000_abcdef"), None)
        if stream_callback:
            stream_callback("")
            stream_callback("正在分析项目结构……")
        request = SimpleNamespace(
            approval_id="approval-1",
            tool="shell",
            risk="medium",
            summary="pytest --api-key superSecret123",
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
    assert waiting["stream_text"] == "正在分析项目结构……"
    assert waiting["pending_approval"]["approval_id"] == "approval-1"
    assert "[REDACTED]" in waiting["pending_approval"]["summary"]
    assert "superSecret123" not in json.dumps(waiting)
    assert "arguments" not in waiting["pending_approval"]
    assert "api_key" not in json.dumps(waiting)

    with pytest.raises(AgentError) as exc:
        manager.approve(created["task_id"], "wrong-id", "approved_once")
    assert exc.value.code == "APPROVAL_STALE"

    result = manager.approve(created["task_id"], "approval-1", "approved_once")
    assert result["status"] == "running"
    service.release.wait(2)
    _wait_for(manager, created["task_id"], "completed")


def test_task_manager_allows_independent_conversations_and_validates_approval():
    service = _FakeService()
    manager = WorkbenchTaskManager(service)
    first = manager.submit("first task", read_only=True)
    service.entered.wait(2)
    _wait_for(manager, first["task_id"], "waiting_approval")

    second = manager.submit("second task", read_only=True)
    _wait_for(manager, second["task_id"], "waiting_approval")
    assert first["task_id"] != second["task_id"]

    with pytest.raises(AgentError) as exc:
        manager.approve(first["task_id"], "approval-1", "approve_everything")
    assert exc.value.code == "REQUEST_INVALID"

    manager.approve(first["task_id"], "approval-1", "denied")
    manager.approve(second["task_id"], "approval-1", "denied")
    service.release.wait(2)
    _wait_for(manager, first["task_id"], "failed")
    _wait_for(manager, second["task_id"], "failed")


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

    def fake_run(task, *, read_only, auto_approve, approval_handler, on_session, stream_callback=None):
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


def test_workbench_root_serves_local_ui_without_exposing_api_data(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    server, _service, _token = create_server(root, port=0, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        with urllib.request.urlopen(base + "/", timeout=3) as response:
            page = response.read().decode("utf-8")
            assert response.status == 200
            assert response.headers.get_content_type() == "text/html"
            assert response.headers.get("X-Frame-Options") == "DENY"
            assert "Jinlin Agent" in page
            assert "开发者详情" in page
            assert "Bearer Token" not in page
            assert "sessionStorage.getItem" not in page
            assert "/api/v1/tasks" in page
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)



def test_unexpected_agent_exception_marks_persisted_session_failed(tmp_path, monkeypatch):
    from jinlin_agent.application import ApplicationService
    from jinlin_agent.config import load_config
    from jinlin_agent.sandbox.null import NullSandbox

    root = tmp_path / "workspace"
    root.mkdir()
    config = load_config(root, home=tmp_path / "jinlin-home")
    monkeypatch.setattr(
        "jinlin_agent.application.create_sandbox",
        lambda workspace, cfg: NullSandbox(str(workspace)),
    )
    monkeypatch.setattr("jinlin_agent.model.create_provider", lambda config, mock_script: object())

    class BrokenHarness:
        def __init__(self, **kwargs):
            pass

        def run(self):
            raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr("jinlin_agent.loop.Harness", BrokenHarness)
    service = ApplicationService(root, config=config)

    with pytest.raises(AgentError) as exc:
        service.run("trigger controlled failure", mock_script="{}")
    assert exc.value.code == "INTERNAL_ERROR"

    sessions = service.list_sessions()
    assert len(sessions) == 1
    assert sessions[0]["status"] == "failed"
    events = service.list_events(sessions[0]["session_id"])
    assert any(event["type"] == "session.failed" for event in events)
