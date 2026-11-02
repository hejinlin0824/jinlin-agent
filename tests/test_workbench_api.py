from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from jinlin_agent.application import ApplicationService
from jinlin_agent.config import load_config
from jinlin_agent.sandbox.null import NullSandbox
from jinlin_agent.session import SessionManager
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


def test_service_reads_session_summary_events_and_stats(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    cfg = load_config(root, home=tmp_path / "jinlin-home")
    session = SessionManager.create(cfg, root, "inspect this project", NullSandbox(str(root)))
    sid = session.id
    session.close()

    service = ApplicationService(root, config=cfg)
    summary = service.get_session(sid)
    assert summary["session_id"] == sid
    assert summary["request"] == "inspect this project"
    assert summary["status"] == "running"
    assert summary["completion_summary"] == ""
    assert summary["event_count"] >= 1
    assert service.list_sessions()[0]["session_id"] == sid
    events = service.list_events(sid, tail=10)
    assert events[0]["type"] == "session.created"
    assert events[0]["request"] == "inspect this project"
    stats = service.stats(sid)
    assert stats["session_id"] == sid
    assert stats["tool_calls"] == 0


def test_http_api_exposes_workspace_scoped_session_read_models(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("JINLIN_HOME", str(tmp_path / "jinlin-home"))
    cfg = load_config(root, home=tmp_path / "jinlin-home")
    session = SessionManager.create(cfg, root, "api summary task", NullSandbox(str(root)))
    sid = session.id
    session.close()

    server, _service, token = create_server(root, port=0, token="test-token")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        status, payload = _request(base + "/api/v1/sessions/" + sid, token)
        assert status == 200 and payload["data"]["request"] == "api summary task"
        status, payload = _request(base + "/api/v1/sessions/" + sid + "/events?tail=5", token)
        assert status == 200 and payload["data"][0]["type"] == "session.created"
        status, payload = _request(base + "/api/v1/sessions/" + sid + "/stats", token)
        assert status == 200 and payload["data"]["session_id"] == sid
        status, payload = _request(base + "/api/v1/sessions", token, headers={"Origin": "http://evil.example"})
        assert status == 403 and payload["error"]["code"] == "ORIGIN_REJECTED"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_service_rejects_session_folder_identity_mismatch(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    cfg = load_config(root, home=tmp_path / "jinlin-home")
    session = SessionManager.create(cfg, root, "identity check", NullSandbox(str(root)))
    original_id = session.id
    session.close()

    forged_id = "sess_20990101_000000_abcdef"
    (cfg.sessions_dir / original_id).rename(cfg.sessions_dir / forged_id)
    service = ApplicationService(root, config=cfg)
    with pytest.raises(AgentError) as exc:
        service.get_session(forged_id)
    assert exc.value.code == "SESSION_NOT_FOUND"
    assert service.list_sessions() == []



@pytest.mark.parametrize("disconnect_error", [
    BrokenPipeError("peer closed"),
    ConnectionResetError("peer reset"),
    ConnectionAbortedError("peer aborted"),
])
def test_json_response_tolerates_client_disconnect(tmp_path, disconnect_error):
    import io

    root = tmp_path / "workspace"
    root.mkdir()
    server, _service, _token = create_server(root, port=0, token="test-token")
    handler = object.__new__(server.RequestHandlerClass)
    calls = []
    handler.send_response = lambda status: calls.append(("status", status))
    handler.send_header = lambda name, value: calls.append(("header", name, value))
    handler.end_headers = lambda: calls.append(("end_headers",))
    
    class DisconnectedWriter:
        def write(self, _body):
            raise disconnect_error

    handler.wfile = DisconnectedWriter()
    try:
        # A disconnected client is a normal transport lifecycle event. The
        # response helper must not raise or attempt a second response.
        assert handler._json(200, {"data": {"ok": True}}) is None
        assert ("status", 200) in calls
        assert calls.count(("end_headers",)) == 1
    finally:
        server.server_close()


def test_html_response_tolerates_client_disconnect(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    server, _service, _token = create_server(root, port=0, token="test-token")
    handler = object.__new__(server.RequestHandlerClass)
    calls = []
    handler.send_response = lambda status: calls.append(("status", status))
    handler.send_header = lambda name, value: calls.append(("header", name, value))
    handler.end_headers = lambda: calls.append(("end_headers",))

    class DisconnectedWriter:
        def write(self, _body):
            raise ConnectionAbortedError("browser navigated away")

    handler.wfile = DisconnectedWriter()
    try:
        handler._html()
        assert ("status", 200) in calls
        assert calls.count(("end_headers",)) == 1
    finally:
        server.server_close()
