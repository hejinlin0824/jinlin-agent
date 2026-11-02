"""Authenticated loopback HTTP API for the local workbench (stdlib only).

WP-1 deliberately exposes read-only APIs. Task submission and approvals are
added in WP-2 after the service lifecycle and UI interaction model are tested.
"""
from __future__ import annotations

import hmac
import ipaddress
import json
import secrets
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from . import __version__
from .application import ApplicationService
from .errors import AgentError

API_PREFIX = "/api/v1"


def _is_loopback_host(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


def create_server(workspace, host: str = "127.0.0.1", port: int = 8765, token: str | None = None):
    """Create a loopback-only server. Returns (server, service, bearer_token)."""
    if not _is_loopback_host(host):
        raise ValueError("Jinlin Workbench only binds to loopback addresses in WP-1")
    service = ApplicationService(workspace)
    bearer = token or secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server_version = "JinlinWorkbench"
        sys_version = ""

        def log_message(self, fmt, *args):
            # Avoid logging request headers, query strings, or user-controlled values.
            return

        def _json(self, status: int, payload: dict):
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _origin_and_host_ok(self) -> bool:
            host_header = self.headers.get("Host", "")
            try:
                host_name = urlsplit("//" + host_header).hostname or ""
            except ValueError:
                return False
            if not _is_loopback_host(host_name):
                return False
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme != "http" or not _is_loopback_host(parsed.hostname or ""):
                    return False
                if parsed.netloc.lower() != host_header.lower():
                    return False
            return True

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = "Bearer " + bearer
            return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

        def do_OPTIONS(self):
            self._json(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "CORS is not enabled"}})

        def do_POST(self):
            self._json(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "WP-1 API is read-only"}})

        def do_PUT(self):
            self._json(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "WP-1 API is read-only"}})

        def do_DELETE(self):
            self._json(405, {"error": {"code": "METHOD_NOT_ALLOWED", "message": "WP-1 API is read-only"}})

        def do_GET(self):
            if not self._origin_and_host_ok():
                self._json(403, {"error": {"code": "ORIGIN_REJECTED", "message": "request host or origin is not allowed"}})
                return
            parsed = urlsplit(self.path)
            if parsed.path == API_PREFIX + "/health":
                self._json(200, {"ok": True, "api_version": 1, "version": __version__})
                return
            if not self._authorized():
                self._json(401, {"error": {"code": "UNAUTHORIZED", "message": "bearer token required"}})
                return
            try:
                parts = [p for p in parsed.path.split("/") if p]
                if parts == ["api", "v1", "doctor"]:
                    self._json(200, {"data": service.doctor()})
                    return
                if parts == ["api", "v1", "sessions"]:
                    self._json(200, {"data": service.list_sessions()})
                    return
                if len(parts) >= 4 and parts[:3] == ["api", "v1", "sessions"]:
                    sid = parts[3]
                    if len(parts) == 4:
                        data = service.get_session(sid)
                    elif len(parts) == 5 and parts[4] == "events":
                        from urllib.parse import parse_qs
                        query = parse_qs(parsed.query, keep_blank_values=False)
                        raw_tail = query.get("tail", ["100"])[0]
                        if not raw_tail.isdigit():
                            raise AgentError("REQUEST_INVALID", "tail must be an integer", source="http")
                        data = service.list_events(sid, int(raw_tail))
                    elif len(parts) == 5 and parts[4] == "stats":
                        data = service.stats(sid)
                    else:
                        self._json(404, {"error": {"code": "NOT_FOUND", "message": "route not found"}})
                        return
                    self._json(200, {"data": data})
                    return
                self._json(404, {"error": {"code": "NOT_FOUND", "message": "route not found"}})
            except AgentError as exc:
                status = 404 if exc.code == "SESSION_NOT_FOUND" else 400
                self._json(status, {"error": {"code": exc.code, "message": exc.message}})
            except Exception:
                self._json(500, {"error": {"code": "INTERNAL_ERROR", "message": "request could not be completed"}})

    class LocalThreadingHTTPServer(ThreadingHTTPServer):
        address_family = socket.AF_INET6 if ":" in host else socket.AF_INET

    server = LocalThreadingHTTPServer((host, int(port)), Handler)
    server.daemon_threads = True
    return server, service, bearer
