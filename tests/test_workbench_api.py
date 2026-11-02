from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from jinlin_agent.application import ApplicationService
from jinlin_agent.errors import AgentError
from jinlin_agent.web import create_server


def test_application_service_pins_workspace_and_lists_only_its_sessions(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    service = ApplicationService(root)
    assert service.workspace == root.resolve()
    assert service.list_sessions() == []
    with pytest.raises(AgentError):
        ApplicationService(root / "missing")


def test_application_service_rejects_invalid_session_ids(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    service = ApplicationService(root)
    with pytest.raises(AgentError) as exc:
        service.get_session("../../etc/passwd")
    assert exc.value.code == "SESSION_NOT_FOUND"


def test_event_projection_does_not_expose_raw_tool_arguments_or_output():
    event = {
        "seq": 7, "ts": "2026-01-01T00:00:00Z", "type": "tool.intent",
        "payload": {"tool": "shell", "args": {"command": "echo secret"}, "api_key": "do-not-leak"},
    }
    result = ApplicationService._event_summary(event)
    assert result["tool"] == "shell"
    assert "args" not in result
    assert "api_key" not in result


def _request(url, token=None, headers=None):
    request_headers = dict(headers or {})
    if token:
        request_headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_local_http_api_health_auth_and_read_only_methods(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    server, _service, token = create_server(root, port=0, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        status, payload = _request(base + "/api/v1/health")
        assert status == 200 and payload["ok"] is True
        status, payload = _request(base + "/api/v1/sessions")
        assert status == 401 and payload["error"]["code"] == "UNAUTHORIZED"
        status, payload = _request(base + "/api/v1/sessions", token)
        assert status == 200 and payload["data"] == []
        req = urllib.request.Request(base + "/api/v1/sessions", data=b"{}", method="POST",
                                     headers={"Authorization": "Bearer " + token})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(req, timeout=3)
        assert exc.value.code == 405
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_server_refuses_non_loopback_bind(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    with pytest.raises(ValueError):
        create_server(root, host="0.0.0.0")
