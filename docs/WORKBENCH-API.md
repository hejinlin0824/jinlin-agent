# Local Workbench API — WP-1 / WP-2

Status: **WP-1 read models plus WP-2 task/approval API and first local browser dashboard**. This is a local internal API, not a public network API.

## Runtime and dependency policy

- Python standard library only; no third-party runtime dependency is introduced.
- Start with `jinlin-agent serve --workspace PATH [--host 127.0.0.1] [--port 8765]`.
- The server is pinned to one existing workspace for its lifetime.
- Default bind is IPv4 loopback. Explicit IPv6 loopback is supported. Non-loopback addresses are rejected.
- A fresh random Bearer Token is generated on every start and printed once to the terminal. It is not written to disk. The browser UI receives it only through an HttpOnly, SameSite=Strict cookie; JavaScript does not read or store the credential.
- Open `http://127.0.0.1:8765/` for the browser dashboard; the page authenticates automatically through the HttpOnly cookie. The terminal token remains available for direct API clients.
- Press Ctrl+C to stop the server.

## Browser dashboard

The root route serves a packaged, dependency-free HTML dashboard. It shows the pinned workspace, recent sessions, task input, read-only toggle, task status, sanitized event timeline, and pending-approval card. The UI does not display, embed, or store the Bearer Token in JavaScript or browser storage. The server sets a same-origin HttpOnly cookie when serving the page. The page polls the task/session APIs and does not expose a workspace path picker, model-secret editor, arbitrary shell console, or direct file-write control.

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
| GET | `/api/v1/tasks/{task_id}` | Cookie or Bearer | One task's status, session ID, streamed assistant text and safe pending-approval projection |
| POST | `/api/v1/tasks` | Bearer + valid local Origin when supplied | Submit a task; JSON: `{"task":"...","read_only":true}`; defaults to read-only |
| POST | `/api/v1/tasks/{task_id}/approvals/decision` | Bearer + valid local Origin when supplied | Resolve exactly the currently pending approval; JSON: `{"approval_id":"...","decision":"approved_once"}` |

Approval decisions are `approved_once`, `approved_for_session`, or `denied`. A decision is accepted only when the task is actively waiting for the exact approval ID. The API never accepts `auto_approve`, arbitrary tool arguments, a workspace path, or a shell command.

## Task lifecycle and current limits

- Task execution runs on a daemon background thread and calls `ApplicationService.run`, which in turn uses the existing Harness, PolicyEngine, ToolDispatcher, Sandbox and CompletionGate.
- Independent Workbench tasks may run concurrently. Each browser conversation tracks its own task and session; switching conversations does not cancel or replace another task.
- New tasks default to `read_only=true`. A caller may explicitly request `read_only=false`, but the normal PolicyEngine approval rules still apply. The API never auto-approves.
- Read-only turns use a lightweight L0 sandbox object and skip Windows restricted-token/ACL setup, startup project/test discovery, git snapshot subprocesses, baseline test execution, plan bookkeeping and the code-change completion gate. Only observational tools (plus explicit user clarification) are exposed and permitted. Write-capable tasks retain the normal sandbox and completion-verification path.
- Approval prompts expose only approval ID, tool, risk, summary, reason and impact. Raw tool arguments are deliberately withheld.
- The task registry is currently in memory. Event logs and sessions remain durable, but after a server restart the in-memory task IDs are gone; inspect/resume durable sessions through the existing session APIs/CLI. Durable task orchestration, cooperative cancellation, resume, user-question replies, and SSE are later WP-2 increments.
- The browser uses bounded polling to refresh each active task and session. Model text deltas are exposed as a capped `stream_text` field in the task projection; this is incremental polling, not an SSE transport.

## Security model

1. The server only binds loopback. It rejects `0.0.0.0`, LAN IPs, and public IPs.
2. All routes except health require the per-process credential, supplied either as `Authorization: Bearer <token>` for API clients or as the HttpOnly cookie for the same-origin browser UI; comparison uses constant-time comparison.
3. Host must resolve syntactically to loopback/localhost. If Origin is supplied, it must be HTTP loopback and match Host exactly. No CORS headers are emitted.
4. The workspace is resolved and fixed when the service is constructed. No API parameter can select another workspace.
5. Session IDs are format-validated. Resolved session directories and event files must remain directly inside the configured sessions root; symlink escapes are rejected.
6. Event responses and approval prompts are explicit allowlisted projections. Raw tool arguments, raw tool output, arbitrary event payloads and credentials are not serialized.
7. The only current POST operations are task submission and pending-approval decisions. There are no HTTP routes for arbitrary file edits, shell execution, config mutation, Rescue mutation, Git push, or merge.
8. Request bodies are JSON objects capped at 64 KiB. Unknown task fields are rejected; `auto_approve` is not accepted.
9. The token is an access credential. Do not share it or expose it in logs/screenshots. Loopback binding is not a substitute for OS account isolation against other processes running as the same user.

## Compatibility

The existing `jinlin-agent run/resume` CLI continues to use the canonical Harness and delegates execution to `ApplicationService`. Existing CLI command syntax remains unchanged. The HTTP API is additive and does not replace the CLI.


### Final answer and task lifecycle

Read-only investigations and ordinary engineering questions may finish with a plain-text model answer. The Harness persists that answer as the session completion summary and stops the loop; it does not ask the model to call `propose_completion` just to end a conversation. Tasks that changed workspace files still require the completion gate and verification evidence. Truncated model responses are not treated as final answers.

The task projection includes a user-facing `progress` string for session creation, context preparation, model requests, and tool execution. `stream_text` is the current model turn's incremental text. It also includes a `timings` object with `session_created_ms`, `first_model_request_ms`, `first_tool_ms`, `first_token_ms`, and `total_ms`; these are elapsed milliseconds from task submission and are intended for diagnosing startup and response latency. A timing field remains null if its milestone has not occurred. The browser renders common Markdown blocks (headings, lists, code fences, quotes, emphasis and links) using DOM text nodes; raw HTML from model output is never interpreted.
