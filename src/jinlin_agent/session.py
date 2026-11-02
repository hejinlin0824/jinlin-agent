"""Session lifecycle: create, open, lock, snapshot, resume."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .artifacts import ArtifactStore
from .config import Config
from .domain import Status, utcnow_iso
from .errors import SESSION_CORRUPTED, AgentError
from .events import EventLog, new_session_id, read_events
from .state import SessionState, recover
from .verify import detect_project


@dataclass
class Session:
    id: str
    dir: Path
    log: EventLog
    artifacts: ArtifactStore
    sandbox: Any
    config: Config
    workspace: Path
    state: SessionState = field(default_factory=SessionState)

    # ------------------------------------------------------------------ event api

    def emit(self, type: str, payload: dict[str, Any] | None = None, turn_id: str = "", step_id: str = "") -> dict[str, Any]:
        event = self.log.append(type, payload or {}, turn_id, step_id)
        from .state import apply_event

        apply_event(self.state, event)
        return event

    def close(self) -> None:
        try:
            (self.dir / "session.json").write_text(
                json.dumps(
                    {
                        "session_id": self.id,
                        "workspace": str(self.workspace),
                        "status": self.state.status,
                        "phase": self.state.phase,
                        "updated_at": utcnow_iso(),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass
        self.log.close()


class SessionManager:
    @staticmethod
    def create(
        config: Config,
        workspace: Path,
        request: str,
        sandbox: Any,
        session_id: str | None = None,
        *,
        read_only: bool = False,
        task_contract: TaskContract | None = None,
    ) -> Session:
        task_contract = task_contract or TaskContract.legacy(read_only=read_only)
        read_only = task_contract.read_only
        workspace = Path(os.path.abspath(workspace))
        sid = session_id or new_session_id()
        session_dir = config.sessions_dir / sid
        session_dir.mkdir(parents=True, exist_ok=True)
        log = EventLog(session_dir, sid).open()
        artifacts = ArtifactStore(session_dir / "artifacts")
        session = Session(
            id=sid,
            dir=session_dir,
            log=log,
            artifacts=artifacts,
            sandbox=sandbox,
            config=config,
            workspace=workspace,
            state=SessionState(session_id=sid, workspace=str(workspace)),
        )
        session.emit(
            "session.created",
            {
                "session_id": sid,
                "workspace": str(workspace),
                "request": request,
                "model": config.model.model,
                "config_hash": config.effective_hash(),
                "started_at": utcnow_iso(),
                "sandbox_level": getattr(sandbox, "level", "L0"),
                "read_only": bool(read_only),
                "task_contract": task_contract.to_dict(),
                "source_configs": config.sources,
            },
        )
        for violation in config.violations:
            session.emit("config.escalation_blocked", violation)
        session.emit("sandbox.mode", sandbox.describe())
        # Read-only questions must not pay for project-wide verification
        # discovery, git subprocess probes, or baseline tests before the first
        # model request. The model inspects the workspace through read tools.
        snapshot = workspace_snapshot(
            workspace, config, sandbox,
            include_project=not read_only,
            include_git=not read_only,
        )
        session.emit("workspace.snapshot", {"snapshot": snapshot})
        if read_only:
            session.emit(
                "verification.baseline",
                {"baseline": {
                    "status": "skipped",
                    "reason": "read-only session: baseline commands are not executed",
                    "command": None,
                }},
            )
        else:
            SessionManager._capture_baseline(session)
        return session

    @staticmethod
    def _capture_baseline(session: Session) -> None:
        """Record which tests already fail, so the gate can tell regressions from pre-existing failures."""
        from .verify import Verifier

        if not session.config.verification.get("run_baseline", True):
            session.emit(
                "verification.baseline",
                {"baseline": {"status": "skipped", "reason": "baseline disabled in config", "command": None}},
            )
            return
        try:
            verifier = Verifier(
                session.workspace,
                session.config,
                session.sandbox,
                session.artifacts,
                emit=lambda t, p: session.emit(t, p),
                state=session.state,
            )
            if verifier.project.test_commands:
                verifier.ensure_baseline()
            else:
                session.emit(
                    "verification.baseline",
                    {"baseline": {"status": "skipped", "reason": "no test command detected", "command": None}},
                )
        except Exception as exc:
            session.emit(
                "verification.baseline",
                {
                    "baseline": {
                        "status": "error",
                        "error": f"{type(exc).__name__}: {exc}",
                        "command": None,
                    }
                },
            )

    @staticmethod
    def open(config: Config, session_id: str, sandbox: Any) -> Session:
        session_dir = config.sessions_dir / session_id
        if not (session_dir / "events.jsonl").exists():
            raise AgentError(SESSION_CORRUPTED, f"no session found: {session_id}", source="session")
        log = EventLog(session_dir, session_id).open()
        artifacts = ArtifactStore(session_dir / "artifacts")
        events = list(read_events(session_dir))
        state = recover(events)
        workspace = Path(state.workspace or ".")
        session = Session(
            id=session_id,
            dir=session_dir,
            log=log,
            artifacts=artifacts,
            sandbox=sandbox,
            config=config,
            workspace=workspace,
            state=state,
        )
        session.emit("session.resumed", {"from_seq": log.last_seq, "previous_status": state.status})
        SessionManager._resolve_orphan_intents(session, events)
        return session

    @staticmethod
    def _resolve_orphan_intents(session: Session, events: list[dict[str, Any]]) -> None:
        """A tool.intent without a result means we crashed mid-call: probe and be honest."""
        intents: dict[str, dict[str, Any]] = {}
        resolved: set[str] = set()
        for evt in events:
            payload = evt.get("payload") or {}
            if evt.get("type") == "tool.intent":
                intents[payload.get("tool_call_id", "")] = payload
            elif evt.get("type") in ("tool.completed", "tool.failed", "tool.denied"):
                resolved.add(payload.get("tool_call_id", ""))
        for call_id, intent in intents.items():
            if call_id in resolved:
                continue
            tool = intent.get("tool", "?")
            args = intent.get("args") or {}
            uncertain = True
            if tool in ("read_file", "list_dir", "find_files", "search_text", "get_file_info", "get_project_summary"):
                uncertain = False
            session.emit(
                "tool.failed",
                {
                    "tool_call_id": call_id,
                    "tool": tool,
                    "error": {
                        "code": "RECOVERY_UNCERTAIN",
                        "message": (
                            f"session resumed with an unfinished {tool} call; the harness cannot prove whether it "
                            f"took effect ({'read-only, treated as safe' if not uncertain else 'side-effect, verify the workspace before continuing'})"
                        ),
                        "retriable": True,
                        "source": "recovery",
                        "details": {"args": args},
                    },
                    "duration_ms": 0,
                },
            )

    @staticmethod
    def list_sessions(config: Config) -> list[dict[str, Any]]:
        root = config.sessions_dir
        if not root.exists():
            return []
        out: list[dict[str, Any]] = []
        for entry in sorted(root.iterdir(), reverse=True):
            meta = entry / "session.json"
            events = entry / "events.jsonl"
            if not events.exists():
                continue
            info: dict[str, Any] = {"session_id": entry.name, "updated_at": ""}
            if meta.exists():
                try:
                    info.update(json.loads(meta.read_text(encoding="utf-8")))
                except Exception:
                    pass
            state = recover(read_events(entry))
            info["status"] = state.status
            info["phase"] = state.phase
            info["request"] = (state.request or "")[:120]
            out.append(info)
        return out


def workspace_snapshot(
    workspace: Path,
    config: Config,
    sandbox: Any,
    *,
    include_project: bool = True,
    include_git: bool = True,
) -> dict[str, Any]:
    workspace = Path(workspace)
    project = detect_project(workspace, config) if include_project else None
    snapshot: dict[str, Any] = {
        "workspace_root": str(workspace),
        "created_at": utcnow_iso(),
        "project_types": project.types if project else [],
        "test_commands": [" ".join(c) for c in project.test_commands] if project else [],
        "lint_commands": [" ".join(c) for c in project.lint_commands] if project else [],
        "instruction_files": project.instruction_files if project else [],
        "git_repo": False,
        "branch": "",
        "head": "",
        "dirty_files": [],
        "status_lines": [],
    }
    if include_git:
        res = sandbox.run(["git", "status", "--porcelain=v1", "-b"], str(workspace), timeout=30)
        if res.exit_code == 0:
            lines = [l for l in (res.stdout or "").splitlines() if l.strip()]
            snapshot["git_repo"] = True
            if lines and lines[0].startswith("##"):
                snapshot["branch"] = lines[0].lstrip("# ").strip()
                lines = lines[1:]
            snapshot["status_lines"] = lines[:200]
            snapshot["dirty_files"] = [l[3:].strip().strip('"') for l in lines if len(l) > 3]
            head = sandbox.run(["git", "rev-parse", "--short", "HEAD"], str(workspace), timeout=30)
            if head.exit_code == 0:
                snapshot["head"] = (head.stdout or "").strip()
    return snapshot
