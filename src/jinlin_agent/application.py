"""Application service boundary shared by the CLI and local HTTP clients.

The service owns workspace scoping and read models. Execution remains delegated
to the existing SessionManager, Harness, sandbox and CompletionGate.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from .config import Config, load_config
from .errors import AgentError
from .sandbox import create_sandbox
from .session import SessionManager, workspace_snapshot
from .state import recover
from .events import read_events

_SESSION_ID = re.compile(r"^sess_\d{8}_\d{6}_[0-9a-f]{6}$")


class ApplicationService:
    """Use-case API for CLI and local workbench adapters.

    A service instance is pinned to one existing workspace. It never accepts a
    workspace path from an API request, preventing a browser client from
    expanding the server's authority.
    """

    def __init__(self, workspace: str | Path, config: Config | None = None) -> None:
        root = Path(workspace).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise AgentError("CONFIG_INVALID", f"workspace is not a directory: {root}", source="application")
        self.workspace = root
        self.config = config or load_config(root)

    def doctor(self) -> dict[str, Any]:
        sandbox = create_sandbox(self.workspace, self.config)
        snapshot = workspace_snapshot(self.workspace, self.config, sandbox)
        return {
            "workspace": str(self.workspace),
            "python": __import__("sys").version.split()[0],
            "model": {
                "provider": self.config.model.provider,
                "model": self.config.model.model,
                "base_url": self.config.model.base_url,
                "api_key_configured": bool(self.config.api_key()),
                "api_key_env": self.config.model.api_key_env,
            },
            "sessions_dir": str(self.config.sessions_dir),
            "sandbox": {
                "level": getattr(sandbox, "level", "L0"),
                "engine": getattr(sandbox, "engine", "none"),
                "details": sandbox.describe(),
            },
            "workspace_snapshot": snapshot,
            "config_sources": list(self.config.sources),
            "config_violations": list(self.config.violations),
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = SessionManager.list_sessions(self.config)
        root = str(self.workspace)
        return [row for row in rows if str(Path(row.get("workspace") or "").resolve()) == root]

    def _session_dir(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise AgentError("SESSION_NOT_FOUND", "invalid session id", source="application")
        root = self.config.sessions_dir.resolve()
        path = (root / session_id).resolve()
        if path.parent != root or not (path / "events.jsonl").is_file():
            raise AgentError("SESSION_NOT_FOUND", "session not found", source="application")
        return path

    def _events(self, session_id: str) -> tuple[Path, list[dict[str, Any]], Any]:
        directory = self._session_dir(session_id)
        events = list(read_events(directory))
        state = recover(events)
        if str(Path(state.workspace or "").resolve()) != str(self.workspace):
            raise AgentError("SESSION_NOT_FOUND", "session does not belong to this workspace", source="application")
        return directory, events, state

    def get_session(self, session_id: str) -> dict[str, Any]:
        directory, events, state = self._events(session_id)
        return {
            "session_id": state.session_id,
            "workspace": str(self.workspace),
            "request": state.request,
            "status": state.status,
            "phase": state.phase,
            "step_index": state.step_index,
            "last_seq": state.last_seq,
            "started_at": state.started_at,
            "updated_at": state.updated_at,
            "model": state.model,
            "sandbox_level": state.sandbox_level,
            "pending_approval": bool(state.pending_approval),
            "pending_question": bool(state.pending_question),
            "verification_runs": [
                {"id": v.verification_id, "level": v.level, "type": v.type, "status": v.status}
                for v in state.verification_runs
            ],
            "changed_files": state.diff_paths(),
            "report_available": bool(state.report_artifact_id and (directory / "report.md").is_file()),
            "event_count": len(events),
        }

    def list_events(self, session_id: str, tail: int = 100) -> list[dict[str, Any]]:
        if not isinstance(tail, int) or tail < 1 or tail > 1000:
            raise AgentError("REQUEST_INVALID", "tail must be between 1 and 1000", source="application")
        _directory, events, _state = self._events(session_id)
        return [self._event_summary(evt) for evt in events[-tail:]]

    @staticmethod
    def _event_summary(event: dict[str, Any]) -> dict[str, Any]:
        """Expose an intentionally narrow event projection, never raw tool args/output."""
        payload = event.get("payload") or {}
        result: dict[str, Any] = {
            "seq": event.get("seq"),
            "timestamp": event.get("ts"),
            "type": event.get("type"),
        }
        etype = event.get("type")
        if etype == "session.created":
            result["request"] = str(payload.get("request", ""))[:500]
        elif etype in ("tool.intent", "tool.completed", "tool.failed", "tool.denied"):
            result["tool"] = payload.get("tool")
            result["tool_call_id"] = payload.get("tool_call_id")
            if etype in ("tool.failed", "tool.denied"):
                result["error_code"] = (payload.get("error") or {}).get("code")
        elif etype == "verification.completed":
            verification = payload.get("verification") or {}
            result.update({
                "verification_id": verification.get("verification_id"),
                "level": verification.get("level"),
                "status": verification.get("status"),
            })
        elif etype in ("session.failed", "session.cancelled"):
            result["error_code"] = (payload.get("error") or {}).get("code")
        return result

    def stats(self, session_id: str) -> dict[str, Any]:
        _directory, _events, state = self._events(session_id)
        return {
            "session_id": state.session_id,
            "status": state.status,
            "phase": state.phase,
            "steps": state.step_index,
            "model_calls": state.model_calls,
            "tool_calls": state.tool_calls_total,
            "tokens_in": state.tokens_in,
            "tokens_out": state.tokens_out,
            "compactions": state.compactions,
            "loop_events": state.loop_events,
            "verification_runs": len(state.verification_runs),
            "final_result": (state.completion or {}).get("result"),
            "report_artifact_id": state.report_artifact_id,
        }

    def run(
        self,
        task: str,
        *,
        read_only: bool = False,
        auto_approve: bool = False,
        approval_handler: Callable[[Any], tuple[str, str]] | None = None,
        mock_script: str | None = None,
        log: Callable[[str], None] | None = None,
        stream_callback: Callable[[str], None] | None = None,
        on_session: Callable[[Any, Any], None] | None = None,
    ) -> tuple[Any, str]:
        """Run a task through the canonical Harness; caller owns presentation."""
        if not isinstance(task, str) or not task.strip():
            raise AgentError("REQUEST_INVALID", "task must be a non-empty string", source="application")
        if not self.config.api_key() and not mock_script:
            raise AgentError("CONFIG_INVALID", f"no API key configured ({self.config.model.api_key_env})", source="application")
        sandbox = create_sandbox(self.workspace, self.config)
        session = SessionManager.create(self.config, self.workspace, task.strip(), sandbox)
        if on_session:
            on_session(session, sandbox)
        try:
            from .loop import Harness
            from .model import create_provider

            harness = Harness(
                session=session,
                config=self.config,
                provider=create_provider(self.config, mock_script),
                approval_handler=approval_handler,
                auto_approve=bool(auto_approve),
                read_only=bool(read_only),
                stream_callback=stream_callback,
                log=log,
            )
            status = harness.run()
            return session, status
        finally:
            session.close()

    def resume(
        self,
        session_id: str,
        *,
        answer: str = "",
        read_only: bool = False,
        auto_approve: bool = False,
        approval_handler: Callable[[Any], tuple[str, str]] | None = None,
        mock_script: str | None = None,
        log: Callable[[str], None] | None = None,
        stream_callback: Callable[[str], None] | None = None,
    ) -> tuple[Any, str]:
        directory = self._session_dir(session_id)
        events = list(read_events(directory))
        previous = recover(events)
        if str(Path(previous.workspace or "").resolve()) != str(self.workspace):
            raise AgentError("SESSION_NOT_FOUND", "session does not belong to this workspace", source="application")
        sandbox = create_sandbox(self.workspace, self.config)
        session = SessionManager.open(self.config, session_id, sandbox)
        if answer:
            session.emit("session.user_input", {"text": answer})
        try:
            from .loop import Harness
            from .model import create_provider

            harness = Harness(
                session=session,
                config=self.config,
                provider=create_provider(self.config, mock_script),
                approval_handler=approval_handler,
                auto_approve=bool(auto_approve),
                read_only=bool(read_only),
                stream_callback=stream_callback,
                log=log,
            )
            status = harness.run()
            return session, status
        finally:
            session.close()
