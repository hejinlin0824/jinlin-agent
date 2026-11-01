# AGENTS.md

Guidance for agents (human or model) working in this repository.

## What this project is

A code-engineer agent harness. The value is in the **execution semantics** (event
sourcing, deterministic control, verification), not in the number of tools.

## Hard rules

1. Zero third-party runtime dependencies (`src/` must import stdlib only).
2. Every observable side effect goes through `ToolDispatcher` and lands in the
   event log as `tool.intent` before execution and `tool.completed/failed` after.
3. The model may propose completion; only `CompletionGate` decides. Never add a
   code path that lets a tool mark a session completed.
4. Security-relevant config is layered and **project-level may only tighten**.
   Do not add a bypass.
5. Editing is exact-match only. Do not introduce fuzzy patching.
6. Never claim a test passed without a `VerificationRun` in the event log.
7. Every filesystem entry point (file tools, edit engine, shell cwd) must use
   `paths.resolve_in_workspace`; never re-implement the boundary check. A path
   is inside the workspace only if its **real** path is (symlinks and Windows
   junctions included, even when the final target does not exist yet).
8. Existing files may only be edited after a session read + unchanged hash; the
   final replace must re-check the hash (`atomic_write(..., expected_sha256=)`).
9. A claim about tests must cite a **passing L3 verification** (ver_* id,
   artifact_* id, "L3 tests", or the test command) — a file path is not evidence
   that tests passed. L0 failures must block completion.
10. The event log is the only truth: tolerate a torn tail, reject mid-log
    corruption and sequence gaps (`SESSION_CORRUPTED`).

## Testing

```bash
python -m pytest -q                 # unit + probes + review regressions (64 tests)
python -m jinlin_agent eval --suite all
```

- New tool: add `ToolSpec` + handler + a unit test; keep the description explicit
  about when *not* to use it.
- New event type: add to `state._HANDLERS` and document it in `docs/PLAN.md`.
- New probe: add to `src/jinlin_agent/evalkit.py` and it is picked up by
  `tests/test_eval.py` automatically.

## Layout conventions

- `domain.py` contains serializable dataclasses only.
- Tools return `ToolResult`; they must not append events directly (use `extra`
  keys: `plan_event`, `fact_event`, `decision_event`, `completion`,
  `request_user_input`).
- Errors are `AgentError(code, message, retriable, source)`; codes are in
  `errors.py`.
