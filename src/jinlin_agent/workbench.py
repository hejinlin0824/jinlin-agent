"""Workbench (v2.0 WP-V2.0-9): task observability over the Task Ledger.

Read-only endpoints by default; the ONLY writes are task cancel and answer
(resume a waiting worker), and only when a live WorkerRuntime is attached.
Bound to 127.0.0.1. HTTP approvals are deliberately NOT exposed: approvals
are fail-closed interactive (v2 §9.6), and an unauthenticated local HTTP
approval endpoint would be a policy bypass. The page never invents progress:
everything shown is read from the ledger and the task event logs (v2 §14-WP9
acceptance: the page matches the ledger, survives refresh/restart, and does
not display fake realtime state).
"""

from __future__ import annotations

import hmac
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .events import read_events
from .ledger import TaskLedger

_INDEX_HTML = """<!doctype html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="workbench-token" content="{{TOKEN}}">
<title>jinlin-agent workbench</title>
<style>
  body { font-family: -apple-system, "Segoe UI", sans-serif; margin: 24px; background: #111; color: #ddd; }
  h1 { font-size: 18px; } table { border-collapse: collapse; width: 100%; margin-top: 12px; }
  th, td { border: 1px solid #333; padding: 6px 10px; font-size: 13px; text-align: left; }
  th { background: #1b1b1b; } tr:hover td { background: #181818; }
  .status { font-weight: 600; } .ok { color: #7bd88f; } .bad { color: #f2777a; } .wait { color: #e5c07b; }
  button { background: #333; color: #ddd; border: 1px solid #444; padding: 3px 10px; cursor: pointer; }
  input { background: #1b1b1b; color: #ddd; border: 1px solid #444; padding: 3px 6px; }
  #detail { margin-top: 18px; white-space: pre-wrap; font-family: Consolas, monospace; font-size: 12px; }
</style>
</head>
<body>
<h1>jinlin-agent workbench — task ledger</h1>
<table id="tasks"><thead><tr>
  <th>task</th><th>title</th><th>status</th><th>root</th><th>deps</th><th>verification</th><th>actions</th>
</tr></thead><tbody></tbody></table>
<div id="detail"></div>
<script>
// WP-R3: untrusted task data is rendered with textContent-only DOM
// construction — hostile task titles or messages can never execute
// script in this page (guarded by tests). The per-start token gates every /api call.
const TOKEN = document.querySelector('meta[name="workbench-token"]').content;
const BAD = new Set(["failed", "cancelled", "blocked"]);
const WAIT = new Set(["waiting_user", "waiting_approval", "queued", "review_pending"]);
function cls(s) { return BAD.has(s) ? "bad" : WAIT.has(s) ? "wait" : "ok"; }
function cell(text) { const td = document.createElement("td"); td.textContent = String(text); return td; }
function api(path, options) {
  options = options || {};
  options.headers = Object.assign({ "Authorization": "Bearer " + TOKEN }, options.headers || {});
  return fetch(path, options);
}
async function load() {
  const data = await (await api("/api/tasks")).json();
  const tb = document.querySelector("#tasks tbody");
  tb.textContent = "";
  for (const t of data.tasks) {
    const tr = document.createElement("tr");
    tr.appendChild(cell(t.task_id));
    tr.appendChild(cell(t.title));
    const st = cell(t.status); st.className = "status " + cls(t.status); tr.appendChild(st);
    tr.appendChild(cell(t.root_task_id));
    tr.appendChild(cell((t.dependencies || []).join(", ")));
    tr.appendChild(cell(t.verification || ""));
    const act = document.createElement("td");
    const bDetail = document.createElement("button"); bDetail.textContent = "detail"; bDetail.dataset.taskId = t.task_id; bDetail.addEventListener("click", function () { detail(t.task_id); });
    const bCancel = document.createElement("button"); bCancel.textContent = "cancel"; bCancel.addEventListener("click", function () { cancel(t.task_id); });
    const inp = document.createElement("input"); inp.placeholder = "answer..."; inp.dataset.taskId = t.task_id;
    const bAnswer = document.createElement("button"); bAnswer.textContent = "answer"; bAnswer.addEventListener("click", function () { answer(t.task_id); });
    act.appendChild(bDetail); act.appendChild(bCancel); act.appendChild(inp); act.appendChild(bAnswer);
    tr.appendChild(act);
    tb.appendChild(tr);
  }
  document.querySelector("h1").textContent =
    "jinlin-agent workbench — " + data.summary.total + " task(s), workers=" + data.summary.workers + ", messages=" + data.summary.messages;
}
async function detail(id) {
  const d = await (await api("/api/tasks/" + encodeURIComponent(id))).json();
  document.getElementById("detail").textContent = JSON.stringify(d, null, 2);
}
async function cancel(id) {
  await api("/api/tasks/" + encodeURIComponent(id) + "/cancel", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  load();
}
async function answer(id) {
  const input = document.querySelector('input[data-task-id="' + CSS.escape(id) + '"]');
  const answer = input ? input.value : "";
  await api("/api/tasks/" + encodeURIComponent(id) + "/answer", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ answer }) });
  setTimeout(load, 300);
}
load(); setInterval(load, 3000);
</script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "jinlin-workbench/1.0"

    def log_message(self, *args: Any) -> None:  # silence default stderr chatter
        return

    # ------------------------------------------------------------------ helpers

    # ------------------------------------------------------------------ security (WP-R3)

    def _allowed_origins(self) -> set[str]:
        port = self.server.workbench._httpd.server_address[1]
        return {
            self.server.workbench.base_url,
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
        }

    def _host_ok(self) -> bool:
        """DNS-rebinding defense: the Host header must name this loopback
        server, not a hostile domain that resolved to 127.0.0.1."""
        host = (self.headers.get("Host") or "").lower()
        port = self.server.workbench._httpd.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _origin_ok(self) -> bool:
        """CSRF defense for state-changing calls: a browser-sent Origin must
        be our own origin. Non-browser clients (no Origin) pass, gated by the
        Bearer token instead."""
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        return origin in self._allowed_origins()

    def _token_ok(self) -> bool:
        header = self.headers.get("Authorization") or ""
        expected = f"Bearer {self.server.workbench.token}"
        return hmac.compare_digest(header, expected)

    def _gate(self, *, state_change: bool) -> bool:
        """Common gate: Host always; token on every /api call; Origin on
        state changes. Sends the error response itself."""
        if not self._host_ok():
            self._json(403, {"error": "host not allowed"})
            return False
        if not self._token_ok():
            self._json(401, {"error": "missing or invalid bearer token"})
            return False
        if state_change and not self._origin_ok():
            self._json(403, {"error": "origin not allowed"})
            return False
        return True

    def _send(self, status: int, body: str, content_type: str = "application/json; charset=utf-8") -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False))

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # ------------------------------------------------------------------ routes

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        path = self.path.split("?")[0]
        if not self._host_ok():
            self._json(403, {"error": "host not allowed"})
            return
        if path in ("/", "/index.html"):
            self._send(200, _INDEX_HTML.replace("{{TOKEN}}", self.server.workbench.token), "text/html; charset=utf-8")
            return
        if not self._gate(state_change=False):
            return
        if path == "/api/tasks":
            rows = self.server.workbench.ledger.list_tasks()
            self._json(200, {"summary": self.server.workbench.ledger.summary(), "tasks": rows})
            return
        if path.startswith("/api/tasks/"):
            task_id = path.rsplit("/", 1)[1]
            self._task_detail(task_id)
            return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?")[0]
        if not self._host_ok():
            self._json(403, {"error": "host not allowed"})
            return
        if not self._gate(state_change=True):
            return
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] in ("cancel", "answer"):
            task_id = parts[2]
            if parts[3] == "cancel":
                self._cancel(task_id)
            else:
                self._answer(task_id)
            return
        if path == "/api/tasks":
            self._create_task()
            return
        self._json(404, {"error": "not found"})

    # ------------------------------------------------------------------ handlers

    def _task_detail(self, task_id: str) -> None:
        wb = self.server.workbench
        row = wb.ledger.get_task(task_id)
        if row is None:
            self._json(404, {"error": f"unknown task: {task_id}"})
            return
        log_dir = Path(wb.config.tasks_dir) / (row.get("root_task_id") or task_id)
        events: list[dict[str, Any]] = []
        try:
            events = list(read_events(log_dir))[-50:]
        except Exception as exc:  # a torn/corrupt task log must not kill the page
            events = [{"type": "log.unreadable", "payload": {"error": str(exc)}}]
        decisions = [
            {
                "type": e.get("type"),
                "ts": e.get("ts"),
                "payload": e.get("payload"),
            }
            for e in events
            if e.get("type") in ("task.strategy_selected", "task.replanned", "phase.changed")
        ]
        self._json(
            200,
            {
                "task": row,
                "workers": wb.ledger.list_workers(task_id),
                "messages": wb.ledger.list_messages(task_id),
                "decisions": decisions,
                "events": events,
            },
        )

    def _create_task(self) -> None:
        """WP-R9: create a root task and drive it on a background thread.
        Requires a supervisor attachment (a provider + config); without one
        the workbench stays read-only."""
        wb = self.server.workbench
        if wb.supervisor is None:
            self._json(405, {"error": "read-only workbench: no supervisor attached"})
            return
        body = self._body()
        title = str(body.get("title", "")).strip()
        if not title:
            self._json(400, {"error": "title must not be empty"})
            return
        description = str(body.get("description", "")).strip()
        workspace = str(body.get("workspace") or wb.config.workspace_root_default)
        from .taskmodel import TaskSpec, new_task_id

        tid = new_task_id()
        spec = TaskSpec(task_id=tid, root_task_id=tid, title=title, description=description, capabilities=["READ", "WRITE", "EXECUTE"])
        ledger = wb.ledger
        ledger.create_task(spec)

        def drive():
            try:
                wb.supervisor.execute_root(spec, Path(workspace))
            except Exception as exc:  # a crashed driver must leave the task visible
                try:
                    ledger.set_task_status(tid, "failed", reason=f"driver crashed: {type(exc).__name__}: {exc}")
                except Exception:
                    pass

        threading.Thread(target=drive, daemon=True).start()
        self._json(201, {"task_id": tid, "status": "created"})

    def _active_worker(self, task_id: str) -> dict[str, Any] | None:
        terminal = {"completed", "failed", "cancelled"}
        rows = [w for w in self.server.workbench.ledger.list_workers(task_id) if w["status"] not in terminal]
        return rows[-1] if rows else None

    def _cancel(self, task_id: str) -> None:
        wb = self.server.workbench
        if wb.runtime is None:
            self._json(405, {"error": "read-only workbench: no runtime attached"})
            return
        if wb.ledger.get_task(task_id) is None:
            self._json(404, {"error": f"unknown task: {task_id}"})
            return
        active = self._active_worker(task_id)
        if active is None:
            self._json(409, {"error": "task has no active worker"})
            return
        wb.runtime.cancel_worker(active["worker_id"], reason="cancelled from the workbench")
        self._json(200, {"cancelled": active["worker_id"]})

    def _answer(self, task_id: str) -> None:
        wb = self.server.workbench
        if wb.runtime is None:
            self._json(405, {"error": "read-only workbench: no runtime attached"})
            return
        if wb.ledger.get_task(task_id) is None:
            self._json(404, {"error": f"unknown task: {task_id}"})
            return
        waiting = [w for w in wb.ledger.list_workers(task_id) if w["status"] in ("waiting_user", "waiting_approval")]
        if not waiting:
            self._json(409, {"error": "task has no waiting worker"})
            return
        body = self._body()
        answer = str(body.get("answer", ""))
        worker = waiting[-1]
        threading.Thread(target=wb.runtime.resume_worker, args=(worker["worker_id"], answer), daemon=True).start()
        self._json(200, {"resumed": worker["worker_id"]})


class WorkbenchServer:
    def __init__(
        self,
        config: Any,
        runtime: Any | None = None,
        port: int = 0,
        ledger: TaskLedger | None = None,
        supervisor: Any | None = None,
    ) -> None:
        self.config = config
        self.runtime = runtime
        self.supervisor = supervisor  # required for POST /api/tasks (task creation)
        self.ledger = ledger if ledger is not None else TaskLedger(config).open()
        # WP-R3: random per-start bearer token for /api/*; delivered to the
        # page via the (same-origin) index HTML meta tag. Not persisted —
        # a restart mints a new one. Loopback-only binding unchanged.
        self.token = secrets.token_urlsafe(32)
        self._httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
        self._httpd.workbench = self  # handler access

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        """Serve on a daemon thread and BLOCK until the port actually accepts
        connections — under a loaded test runner the accept thread may not be
        scheduled before the first client connects (race caught by CI-order
        full-suite runs)."""
        import socket
        import time as _time

        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        host, port = self._httpd.server_address[:2]
        deadline = _time.monotonic() + 10.0
        while _time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=0.25):
                    return
            except OSError:
                _time.sleep(0.02)
        raise RuntimeError(f"workbench server did not become ready on {host}:{port}")

    def serve_forever(self) -> None:
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
