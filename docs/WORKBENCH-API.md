# Local Workbench API — WP-1 / WP-2

Status: **WP-1 read models plus WP-2 task/approval API and first local browser dashboard**. This is a local internal API, not a public network API.

## Runtime and dependency policy

- Python standard library only; no third-party runtime dependency is introduced.
- Start with `jinlin-agent serve --workspace PATH [--host 127.0.0.1] [--port 8765]`.
- The server is pinned to one existing workspace for its lifetime.
- Default bind is IPv4 loopback. Explicit IPv6 loopback is supported. Non-loopback addresses are rejected.
- A fresh random Bearer Token is generated on every start and printed once to the terminal. It is not written to disk.
- Open `http://127.0.0.1:8765/` for the browser dashboard; enter the one-time token printed by the server.
- Press Ctrl+C to stop the server.

## Browser dashboard

The root route serves a packaged, dependency-free HTML dashboard. It shows the pinned workspace, recent sessions, task input, read-only toggle, task status, sanitized event timeline, and pending-approval card. The Bearer Token is entered by the user and kept in the current tab's `sessionStorage`; it is not embedded in the page or written to disk. The page polls the task/session APIs and does not expose a workspace path picker, model-secret editor, arbitrary shell console, or direct file-write control.

## Common response envelope

Success:

```json
{"data": {}}
```

Error:

```json
{"error": {"code": "UNAUTHORIZED", "message": "bearer token required"}}
```

All responses are UTF-8 JSON with `Cache-Control: no-store` and `X-Content-Type-Options: nosniff`.

## Endpoints

| Method | Path | Auth | Response |
|---|---|---|---|
| GET | `/api/v1/health` | No | API and package version |
| GET | `/api/v1/doctor` | Bearer | Workspace, model metadata (never key value), sandbox and project snapshot |
| GET | `/api/v1/sessions` | Bearer | Sessions belonging to the pinned workspace |
| GET | `/api/v1/sessions/{session_id}` | Bearer | Session state summary, verification statuses and changed paths |
| GET | `/api/v1/sessions/{session_id}/events?tail=N` | Bearer | Sanitized event projection; default 100, max 1000 |
| GET | `/api/v1/sessions/{session_id}/stats` | Bearer | Token/tool/verification counters and final result |
| GET | `/api/v1/tasks` | Bearer | Recent in-process Workbench task summaries |
| GET | `/api/v1/tasks/{task_id}` | Bearer | One task's status, session ID and safe pending-approval projection |
| POST | `/api/v1/tasks` | Bearer + valid local Origin when supplied | Submit a task; JSON: `{"task":"...","read_only":true}`; defaults to read-only |
| POST | `/api/v1/tasks/{task_id}/approvals/decision` | Bearer + valid local Origin when supplied | Resolve exactly the currently pending approval; JSON: `{"approval_id":"...","decision":"approved_once"}` |

Approval decisions are `approved_once`, `approved_for_session`, or `denied`. A decision is accepted only when the task is actively waiting for the exact approval ID. The API never accepts `auto_approve`, arbitrary tool arguments, a workspace path, or a shell command.

## Task lifecycle and current limits

- Task execution runs on a daemon background thread and calls `ApplicationService.run`, which in turn uses the existing Harness, PolicyEngine, ToolDispatcher, Sandbox and CompletionGate.
- Only one active Workbench task is allowed per server process. A second submission receives `409 TASK_BUSY`.
- New tasks default to `read_only=true`. A caller may explicitly request `read_only=false`, but the normal PolicyEngine approval rules still apply. The API never auto-approves.
- Approval prompts expose only approval ID, tool, risk, summary, reason and impact. Raw tool arguments are deliberately withheld.
- The task registry is currently in memory. Event logs and sessions remain durable, but after a server restart the in-memory task IDs are gone; inspect/resume durable sessions through the existing session APIs/CLI. Durable task orchestration, cooperative cancellation, resume, user-question replies, and SSE are later WP-2 increments.
- The first implementation uses polling rather than SSE. Clients can poll `GET /tasks/{task_id}` and `GET /sessions/{session_id}`.

## Security model

1. The server only binds loopback. It rejects `0.0.0.0`, LAN IPs, and public IPs.
2. All routes except health require `Authorization: Bearer <token>`; comparison uses constant-time comparison.
3. Host must resolve syntactically to loopback/localhost. If Origin is supplied, it must be HTTP loopback and match Host exactly. No CORS headers are emitted.
4. The workspace is resolved and fixed when the service is constructed. No API parameter can select another workspace.
5. Session IDs are format-validated. Resolved session directories and event files must remain directly inside the configured sessions root; symlink escapes are rejected.
6. Event responses and approval prompts are explicit allowlisted projections. Raw tool arguments, raw tool output, arbitrary event payloads and credentials are not serialized.
7. The only current POST operations are task submission and pending-approval decisions. There are no HTTP routes for arbitrary file edits, shell execution, config mutation, Rescue mutation, Git push, or merge.
8. Request bodies are JSON objects capped at 64 KiB. Unknown task fields are rejected; `auto_approve` is not accepted.
9. The token is an access credential. Do not share it or expose it in logs/screenshots. Loopback binding is not a substitute for OS account isolation against other processes running as the same user.

## Compatibility

The existing `jinlin-agent run/resume` CLI continues to use the canonical Harness and delegates execution to `ApplicationService`. Existing CLI command syntax remains unchanged. The HTTP API is additive and does not replace the CLI.
