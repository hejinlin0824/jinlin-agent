"""Application service boundary shared by the CLI and local HTTP clients.

The service owns workspace scoping and read models. Execution remains delegated
to the existing SessionManager, Harness, sandbox and CompletionGate.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

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
        try:
            root = Path(workspace).expanduser().resolve(strict=True)
        except OSError as exc:
            raise AgentError("CONFIG_INVALID", f"workspace cannot be resolved: {workspace}", source="application") from exc
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

    def list_sessions(self, include_other_workspaces: bool = False) -> list[dict[str, Any]]:
        root = self.config.sessions_dir.resolve()
        if not root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir(), reverse=True):
            if not _SESSION_ID.fullmatch(entry.name) or entry.is_symlink():
                continue
            try:
                directory = entry.resolve(strict=True)
                event_file = (directory / "events.jsonl").resolve(strict=True)
                if directory.parent != root or event_file.parent != directory or event_file.name != "events.jsonl" or not event_file.is_file():
                    continue
                state = recover(read_events(directory))
                if not include_other_workspaces and str(Path(state.workspace or "").resolve()) != str(self.workspace):
                    continue
                meta_path = directory / "session.json"
                updated_at = ""
                if meta_path.is_file() and not meta_path.is_symlink():
                    try:
                        import json
                        updated_at = json.loads(meta_path.read_text(encoding="utf-8")).get("updated_at", "")
                    except (OSError, ValueError, TypeError):
                        pass
                rows.append({
                    "session_id": state.session_id,
                    "workspace": str(self.workspace),
                    "request": (state.request or "")[:120],
                    "status": state.status,
                    "phase": state.phase,
                    "updated_at": updated_at or state.updated_at,
                })
            except (OSError, AgentError, ValueError):
                # A malformed or concurrently changing session is not exposed as a partial row.
                continue
        return rows

    def _session_dir(self, session_id: str) -> Path:
        if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
            raise AgentError("SESSION_NOT_FOUND", "invalid session id", source="application")
        root = self.config.sessions_dir.resolve()
        try:
            path = (root / session_id).resolve(strict=True)
            event_file = (path / "events.jsonl").resolve(strict=True)
        except OSError as exc:
            raise AgentError("SESSION_NOT_FOUND", "session not found", source="application") from exc
        if path.parent != root or event_file.parent != path or event_file.name != "events.jsonl" or not event_file.is_file():
            raise AgentError("SESSION_NOT_FOUND", "session not found", source="application")
        return path

    def _events(self, session_id: str, include_other_workspaces: bool = False) -> tuple[Path, list[dict[str, Any]], Any]:
        directory = self._session_dir(session_id)
        events = list(read_events(directory))
        state = recover(events)
        if not include_other_workspaces and str(Path(state.workspace or "").resolve()) != str(self.workspace):
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

    def trace(self, session_id: str, tail: int = 0, include_other_workspaces: bool = False) -> list[dict[str, Any]]:
        if not isinstance(tail, int) or tail < 0:
            raise AgentError("REQUEST_INVALID", "tail must be a non-negative integer", source="application")
        _directory, events, _state = self._events(session_id, include_other_workspaces=include_other_workspaces)
        return events[-tail:] if tail else events

    def stats(self, session_id: str, include_other_workspaces: bool = False) -> dict[str, Any]:
        _directory, _events, state = self._events(session_id, include_other_workspaces=include_other_workspaces)
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
        try:
            if on_session:
                on_session(session, sandbox)
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
        on_session: Callable[[Any, Any], None] | None = None,
    ) -> tuple[Any, str]:
        directory = self._session_dir(session_id)
        events = list(read_events(directory))
        previous = recover(events)
        if str(Path(previous.workspace or "").resolve()) != str(self.workspace):
            raise AgentError("SESSION_NOT_FOUND", "session does not belong to this workspace", source="application")
        sandbox = create_sandbox(self.workspace, self.config)
        session = SessionManager.open(self.config, session_id, sandbox)
        try:
            if answer:
                session.emit("session.user_input", {"text": answer})
            if on_session:
                on_session(session, sandbox)
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
