# Local Workbench API — WP-1

Status: **WP-1 foundation; read-only API**. This is an internal API contract for the human-facing local workbench. It is not a public network API.

## Runtime and dependency policy

- Python standard library only; no third-party runtime dependency is introduced.
- Start with `jinlin-agent serve --workspace PATH [--host 127.0.0.1] [--port 8765]`.
- The server is pinned to one existing workspace for its lifetime.
- Default bind is IPv4 loopback. Explicit IPv6 loopback is supported. Non-loopback addresses are rejected.
- A fresh random Bearer Token is generated on every start and printed once to the terminal. It is not written to disk.
- Press Ctrl+C to stop the server.

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
| GET | `/api/v1/health` | No | API version and package version |
| GET | `/api/v1/doctor` | Yes | Workspace, model metadata (never key value), sandbox, project snapshot, config source/violations |
| GET | `/api/v1/sessions` | Yes | Sessions belonging to the pinned workspace only |
| GET | `/api/v1/sessions/{session_id}` | Yes | Session state summary, verification statuses, changed paths |
| GET | `/api/v1/sessions/{session_id}/events?tail=N` | Yes | Sanitized event projection; default 100, max 1000 |
| GET | `/api/v1/sessions/{session_id}/stats` | Yes | Token/tool/verification counters and final result |

## Security model

1. The server only binds loopback. It rejects `0.0.0.0`, LAN IPs, and public IPs.
2. All routes except health require `Authorization: Bearer <token>`; comparison uses constant-time comparison.
3. Host must resolve syntactically to loopback/localhost. If Origin is supplied, it must be HTTP loopback and match Host exactly. No CORS headers are emitted.
4. The workspace is resolved and fixed when the service is constructed. No API parameter can select another workspace.
5. Session IDs are format-validated. Resolved session directories and event files must remain directly inside the configured sessions root; symlink escapes are rejected.
6. Event responses are explicit allowlisted projections. Raw tool arguments, raw tool output, arbitrary event payloads and credentials are not serialized.
7. WP-1 is read-only. POST, PUT and DELETE return 405. There is no HTTP path to run shell commands, edit files, approve tools, restore Rescue data, or mutate configuration.
8. The token is an access credential. Do not share it or expose it in logs/screenshots. Loopback binding is not a substitute for OS account isolation against other processes running as the same user.

## Deliberate non-goals in WP-1

No task submission, background task manager, approval endpoints, SSE, static UI, CORS, remote access, multi-workspace server, user accounts, or persisted bearer tokens. These require separate design and tests in later work packages.

## Compatibility

The existing `jinlin-agent run/resume` CLI continues to use the canonical Harness and now delegates execution to `ApplicationService`. Existing CLI command syntax remains unchanged. The HTTP API is additive and does not replace the CLI.
