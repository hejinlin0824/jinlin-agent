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

import json
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
const BAD = new Set(["failed", "cancelled", "blocked"]);
const WAIT = new Set(["waiting_user", "waiting_approval", "queued", "review_pending"]);
function cls(s) { return BAD.has(s) ? "bad" : WAIT.has(s) ? "wait" : "ok"; }
async function load() {
  const data = await (await fetch("/api/tasks")).json();
  const tb = document.querySelector("#tasks tbody");
  tb.innerHTML = "";
  for (const t of data.tasks) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td>${t.task_id}</td><td>${t.title}</td>
      <td class="status ${cls(t.status)}">${t.status}</td><td>${t.root_task_id}</td>
      <td>${(t.dependencies || []).join(", ")}</td><td>${t.verification || ""}</td>
      <td>
        <button onclick="detail('${t.task_id}')">detail</button>
        <button onclick="cancel('${t.task_id}')">cancel</button>
        <input id="ans-${t.task_id}" placeholder="answer..." size="12">
        <button onclick="answer('${t.task_id}')">answer</button>
      </td>`;
    tb.appendChild(tr);
  }
  document.querySelector("h1").textContent =
    `jinlin-agent workbench — ${data.summary.total} task(s), workers=${data.summary.workers}, messages=${data.summary.messages}`;
}
async function detail(id) {
  const d = await (await fetch("/api/tasks/" + id)).json();
  document.getElementById("detail").textContent = JSON.stringify(d, null, 2);
}
async function cancel(id) {
  await fetch(`/api/tasks/${id}/cancel`, {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  load();
}
async function answer(id) {
  const answer = document.getElementById("ans-" + id).value;
  await fetch(`/api/tasks/${id}/answer`, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({answer})});
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
        if path in ("/", "/index.html"):
            self._send(200, _INDEX_HTML, "text/html; charset=utf-8")
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
        parts = path.strip("/").split("/")
        if len(parts) == 4 and parts[:2] == ["api", "tasks"] and parts[3] in ("cancel", "answer"):
            task_id = parts[2]
            if parts[3] == "cancel":
                self._cancel(task_id)
            else:
                self._answer(task_id)
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
        self._json(
            200,
            {
                "task": row,
                "workers": wb.ledger.list_workers(task_id),
                "messages": wb.ledger.list_messages(task_id),
                "events": events,
            },
        )

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
    def __init__(self, config: Any, runtime: Any | None = None, port: int = 0, ledger: TaskLedger | None = None) -> None:
        self.config = config
        self.runtime = runtime
        self.ledger = ledger if ledger is not None else TaskLedger(config).open()
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
