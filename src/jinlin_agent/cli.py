"""Command line interface.

Exit codes: 0 completed, 1 failed, 2 waiting/blocked, 3 cancelled, 4 config error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .config import load_config
from .domain import Status
from .errors import AgentError
from .session import SessionManager, workspace_snapshot
from .sandbox import create_sandbox
from .state import recover


def _utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _print(msg: str = "") -> None:
    print(msg, flush=True)


# --------------------------------------------------------------------------- approval ui


def _interactive_approval(request: Any) -> tuple[str, str]:
    _print("")
    _print("┌─ approval required " + "─" * 46)
    _print(f"│ risk: {request.risk}   tool: {request.tool}")
    _print(f"│ command: {request.summary}")
    _print(f"│ reason: {request.reason}")
    _print("└" + "─" * 68)
    try:
        answer = input("approve? [y]once / [a]ll for session / [N]o: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "denied", "no answer"
    if answer in ("y", "yes"):
        return "approved_once", ""
    if answer in ("a", "all"):
        return "approved_for_session", ""
    return "denied", "user denied"


# --------------------------------------------------------------------------- helpers


def _setup_workspace(args: Any, cfg: Any) -> Path:
    workspace = Path(args.workspace or ".").expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        raise AgentError("CONFIG_INVALID", f"workspace does not exist: {workspace}", source="cli")
    return workspace


def _open_sandbox(cfg: Any, workspace: Path, args: Any) -> Any:
    sandbox = create_sandbox(workspace, cfg)
    level = getattr(sandbox, "level", "L0")
    engine = getattr(sandbox, "engine", "none")
    if level == "L0" and not getattr(args, "quiet", False):
        _print(f"[sandbox] L0 ({engine}) — policy + approval only; no OS-level write isolation")
    elif not getattr(args, "quiet", False):
        _print(f"[sandbox] {level} ({engine})")
    return sandbox


def _make_harness(cfg: Any, session: Any, args: Any) -> Any:
    from .loop import Harness
    from .model import create_provider

    provider = create_provider(cfg, getattr(args, "mock_script", None))
    quiet = getattr(args, "quiet", False)
    streaming = not quiet and sys.stdout.isatty()

    def on_delta(text: str) -> None:
        sys.stdout.write(text)
        sys.stdout.flush()

    def log(msg: str) -> None:
        if not quiet:
            _print(msg)

    approval = None if getattr(args, "yes", False) else (_interactive_approval if sys.stdin.isatty() else None)
    if approval is None and not getattr(args, "yes", False) and not quiet:
        _print("[approval] non-interactive session: risky operations will be denied (fail-closed)")
    harness = Harness(
        session=session,
        config=cfg,
        provider=provider,
        approval_handler=approval,
        auto_approve=bool(getattr(args, "yes", False)),
        read_only=bool(getattr(args, "read_only", False)),
        stream_callback=on_delta if streaming else None,
        log=log,
    )
    return harness


def _exit_for(status: str) -> int:
    return {
        Status.COMPLETED.value: 0,
        Status.FAILED.value: 1,
        Status.WAITING_USER.value: 2,
        Status.WAITING_APPROVAL.value: 2,
        Status.PAUSED.value: 2,
        Status.CANCELLED.value: 3,
    }.get(status, 1)


# --------------------------------------------------------------------------- commands


def cmd_run(args: Any) -> int:
    workspace = Path(args.workspace or ".").expanduser().resolve()
    cfg = load_config(workspace)
    if args.model:
        cfg.model.model = args.model
    if args.base_url:
        cfg.model.base_url = args.base_url
    workspace = _setup_workspace(args, cfg)
    if not cfg.api_key() and not getattr(args, "mock_script", None):
        _print(
            f"[error] no API key. Set ${cfg.model.api_key_env} (or pass --mock-script for a scripted run).",
            file=sys.stderr,
        )
        return 4
    task = args.task or ""
    if not task:
        _print("[task] describe the task (end with a blank line):")
        try:
            lines = []
            while True:
                line = input()
                if not line.strip() and lines:
                    break
                lines.append(line)
            task = "\n".join(lines).strip()
        except (EOFError, KeyboardInterrupt):
            return 3
    if not task:
        _print("[error] empty task", file=sys.stderr)
        return 4
    sandbox = _open_sandbox(cfg, workspace, args)
    session = SessionManager.create(cfg, workspace, task, sandbox)
    _print(f"[session] {session.id}  workspace={workspace}  model={cfg.model.model}")
    _print(f"[task] {task.splitlines()[0][:140]}")
    try:
        harness = _make_harness(cfg, session, args)
        status = harness.run()
    finally:
        session.close()
    return _finish(session, status)


def cmd_resume(args: Any) -> int:
    cfg = load_config(Path(args.workspace or ".").resolve())
    session_dir = cfg.sessions_dir / args.session_id
    if not session_dir.exists():
        _print(f"[error] unknown session: {args.session_id}", file=sys.stderr)
        return 4
    sandbox_holder: dict[str, Any] = {}
    cfg_path = session_dir / "session.json"
    workspace = Path(args.workspace).resolve() if args.workspace else _workspace_from_events(session_dir) or Path(".")
    sandbox = create_sandbox(workspace, cfg)
    session = SessionManager.open(cfg, args.session_id, sandbox)
    previous_status = session.state.status
    if args.answer:
        session.emit("session.user_input", {"text": args.answer})
    _print(f"[session] {session.id} resumed (status was {previous_status})")
    try:
        harness = _make_harness(cfg, session, args)
        status = harness.run()
    finally:
        session.close()
    return _finish(session, status)


def _workspace_from_events(session_dir: Path) -> Path | None:
    state = recover(_iter_events(session_dir))
    return Path(state.workspace) if state.workspace else None


def _iter_events(session_dir: Path):
    from .events import read_events

    return read_events(session_dir)


def _finish(session: Any, status: str) -> int:
    state = session.state
    _print("")
    _print(f"[status] {status}  phase={state.phase}  steps={state.step_index}  "
           f"model_calls={state.model_calls}  tool_calls={state.tool_calls_total}")
    if state.report_artifact_id:
        _print(f"[report] {session.dir / 'report.md'}")
    if state.status == Status.WAITING_USER.value and state.pending_question:
        _print(f"[question] {state.pending_question.get('question', '')}")
        _print(f"[next] jinlin-agent resume {session.id} --answer \"...\"")
    if state.last_error:
        _print(f"[last_error] {state.last_error.get('code')}: {state.last_error.get('message')}")
    return _exit_for(status)


def cmd_sessions(args: Any) -> int:
    cfg = load_config(Path(args.workspace or ".").resolve())
    rows = SessionManager.list_sessions(cfg)
    if not rows:
        _print("(no sessions)")
        return 0
    for row in rows:
        _print(f"{row['session_id']}  {row.get('status','?'):<16} {row.get('phase','?'):<14} {(row.get('request') or '')[:80]}")
    return 0


def cmd_trace(args: Any) -> int:
    cfg = load_config(Path(args.workspace or ".").resolve())
    session_dir = cfg.sessions_dir / args.session_id
    if not session_dir.exists():
        _print(f"[error] unknown session: {args.session_id}", file=sys.stderr)
        return 4
    events = list(_iter_events(session_dir))
    if args.tail:
        events = events[-args.tail:]
    for evt in events:
        payload = evt.get("payload") or {}
        if args.json:
            _print(json.dumps(evt, ensure_ascii=False))
            continue
        summary = _summarize_event(evt.get("type", ""), payload)
        _print(f"{evt.get('seq'):>4} {evt.get('ts','')[11:19]} {evt.get('type',''):<24} {summary}")
    return 0


def _summarize_event(etype: str, payload: dict[str, Any]) -> str:
    if etype == "session.created":
        return payload.get("request", "")[:100]
    if etype == "tool.intent":
        return f"{payload.get('tool')} args={json.dumps(payload.get('args', {}), ensure_ascii=False)[:100]}"
    if etype in ("tool.completed", "tool.failed", "tool.denied"):
        err = payload.get("error") or {}
        return f"{payload.get('tool')} {'ok' if etype == 'tool.completed' else err.get('code','fail')}"
    if etype == "model.attempt":
        return f"usage={payload.get('usage')} finish={payload.get('finish_reason')}"
    if etype == "verification.completed":
        v = payload.get("verification") or {}
        return f"{v.get('level')} {v.get('status')} {v.get('summary')}"
    if etype == "completion.rejected":
        return "; ".join(payload.get("reasons", []))[:140]
    if etype == "session.completed":
        return f"result={payload.get('result')}"
    if etype == "session.failed":
        return (payload.get("error") or {}).get("message", "")[:140]
    if etype == "context.compacted":
        return f"tokens={payload.get('tokens_estimate')} dropped={len(payload.get('dropped', []))}"
    return json.dumps(payload, ensure_ascii=False)[:120]


def cmd_stats(args: Any) -> int:
    cfg = load_config(Path(args.workspace or ".").resolve())
    session_dir = cfg.sessions_dir / args.session_id
    events = list(_iter_events(session_dir))
    if not events:
        _print("[error] no events", file=sys.stderr)
        return 4
    state = recover(events)
    _print(json.dumps(
        {
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
        },
        ensure_ascii=False,
        indent=2,
    ))
    return 0


def cmd_doctor(args: Any) -> int:
    workspace = Path(args.workspace or ".").resolve()
    cfg = load_config(workspace)
    _print(f"python: {sys.version.split()[0]} ({sys.executable})")
    _print(f"workspace: {workspace}")
    _print(f"model: {cfg.model.model} @ {cfg.model.base_url}")
    key = cfg.api_key()
    _print(f"api key: {'set (' + cfg.model.api_key_env + ')' if key else 'MISSING (' + cfg.model.api_key_env + ')'}")
    _print(f"sessions dir: {cfg.sessions_dir}")
    sandbox = create_sandbox(workspace, cfg)
    _print(f"sandbox: level={sandbox.level} engine={sandbox.engine} details={json.dumps(sandbox.describe(), ensure_ascii=False)}")
    snap = workspace_snapshot(workspace, cfg, sandbox)
    _print(f"project types: {snap.get('project_types')}")
    _print(f"test commands: {snap.get('test_commands')}")
    _print(f"git: repo={snap.get('git_repo')} branch={snap.get('branch')} head={snap.get('head')} dirty={len(snap.get('dirty_files', []))}")
    _print(f"config sources: {cfg.sources}")
    if cfg.violations:
        _print(f"config violations: {cfg.violations}")
    return 0


def cmd_clean(args: Any) -> int:
    from .trash import clean_trash

    workspace = Path(args.workspace or ".").expanduser().resolve()
    report = clean_trash(workspace, apply=bool(args.apply))
    _print(f"workspace: {workspace}")
    _print(f"status: {report['status']}")
    if report.get("detail"):
        _print(f"detail: {report['detail']}")
    for key in ("removable", "unrecognized", "removed"):
        items = report.get(key) or []
        _print(f"{key}: {len(items)}")
        for name in items:
            _print(f"  {name}")
    failed = report.get("failed") or []
    _print(f"failed: {len(failed)}")
    for item in failed:
        _print(f"  {item['name']} ({item['error']})")
    if report["status"] == "dry-run":
        _print("dry run only — pass --apply to remove the provable leftovers")
    return 0


def cmd_rescue(args: Any) -> int:
    from .trash import clean_rescue, list_rescue, recover_rescue, restore_rescue

    workspace = Path(args.workspace or ".").expanduser().resolve()
    if args.rescue_action == "recover":
        report = recover_rescue(workspace)
        _print(f"workspace: {workspace}")
        _print(f"status: {report['status']}")
        for item in report.get("items", []):
            _print(f"  {item['status']}: {item.get('stage')} -> {item.get('target')} ({item.get('detail', '')})")
        for item in report.get("orphaned", []):
            _print(f"  orphaned: {item['path']} [{item['kind']}] ({item['detail']})")
        safe = all(x["status"] in ("restored", "already-published") for x in report.get("items", []))
        return 0 if report["status"] == "ok" and safe and not report.get("orphaned") else 2
    if args.rescue_action == "clean":
        report = clean_rescue(workspace, apply=bool(args.apply))
        _print(f"workspace: {workspace}")
        _print(f"status: {report['status']}")
        _print(f"eligible: {len(report['eligible'])}")
        for item in report["eligible"]:
            _print(f"  {item['rescue']} -> {item['target']} (stage={item['stage']}, sha256={item['sha256']})")
        for item in report["failed"]:
            _print(f"  failed: {item}")
        for path in report["removed"]:
            _print(f"  removed: {path}")
        for path in report["manifest_degraded"]:
            _print(f"  manifest update failed after removal: {path}")
        for item in report.get("quarantined", []):
            _print(f"  quarantined evidence (manual inspection required): {item['path']} [{item['kind']}]")
        return 0 if report["status"] in ("dry-run", "applied") and not report["failed"] and not report.get("quarantined") else 2
    if args.rescue_action == "list":
        report = list_rescue(workspace)
        _print(f"workspace: {workspace}")
        _print(f"status: {report['status']}")
        _print(f"verified rescue files: {len(report['rescued'])}")
        for item in report["rescued"]:
            _print(f"  {item['path']} -> {item['orig']} ({item['size']} bytes, {item['integrity']}, sha256={item['sha256']})")
        _print(f"unrecognized rescue files: {len(report['unrecognized'])}")
        for item in report["unrecognized"]:
            _print(f"  {item['path']} (not verified; do not restore automatically)")
        return 0 if report["status"] == "ok" else 2
    result = restore_rescue(workspace, args.path)
    _print(f"status: {result['status']}")
    _print(result.get("detail", ""))
    if result.get("target"):
        _print(f"target: {result['target']}")
        if result.get("rescue"):
            _print(f"rescue copy retained: {result['rescue']}")
        if result.get("stage"):
            _print(f"staging file retained: {result['stage']}")
        if result.get("sha256"):
            _print(f"sha256: {result['sha256']}")
    return 0 if result["status"] in ("restored", "already-published") else 2


def cmd_eval(args: Any) -> int:
    from .evalkit import run_suite

    # eval never writes into the caller's directory unless one is given explicitly
    base = Path(args.workspace).resolve() if args.workspace else None
    return run_suite(args.suite, base, verbose=not args.quiet)


# --------------------------------------------------------------------------- main


def build_parser() -> argparse.ArgumentParser:
    from . import __version__

    parser = argparse.ArgumentParser(prog="jinlin-agent", description="Jinlin Code Engineer Agent")
    parser.add_argument("--version", action="version", version=f"jinlin-agent {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--workspace", default=".")
        p.add_argument("--mock-script", default=None, help="JSON script for a deterministic run")
        p.add_argument("--quiet", action="store_true")

    run = sub.add_parser("run", help="start a new session")
    run.add_argument("task", nargs="?", default="")
    run.add_argument("-t", "--task", dest="task_flag", default=None)
    run.add_argument("--model", default=None)
    run.add_argument("--base-url", default=None)
    run.add_argument("--yes", action="store_true", help="auto-approve medium/high risk (destructive stays denied)")
    run.add_argument("--read-only", action="store_true")
    common(run)
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="resume a session")
    resume.add_argument("session_id")
    resume.add_argument("--answer", default="")
    resume.add_argument("--yes", action="store_true")
    resume.add_argument("--read-only", action="store_true")
    common(resume)
    resume.set_defaults(func=cmd_resume)

    sessions = sub.add_parser("sessions", help="list sessions")
    sessions.add_argument("--workspace", default=".")
    sessions.set_defaults(func=cmd_sessions)

    trace = sub.add_parser("trace", help="print the event log")
    trace.add_argument("session_id")
    trace.add_argument("--tail", type=int, default=0)
    trace.add_argument("--json", action="store_true")
    trace.add_argument("--workspace", default=".")
    trace.set_defaults(func=cmd_trace)

    stats = sub.add_parser("stats", help="session metrics")
    stats.add_argument("session_id")
    stats.add_argument("--workspace", default=".")
    stats.set_defaults(func=cmd_stats)

    doctor = sub.add_parser("doctor", help="environment and config check")
    doctor.add_argument("--workspace", default=".")
    doctor.set_defaults(func=cmd_doctor)

    clean = sub.add_parser("clean", help="list (default) or remove jinlin trash/backup leftovers")
    clean.add_argument("--workspace", default=".", help="workspace to sweep (default: current directory)")
    clean.add_argument(
        "--apply",
        action="store_true",
        help="remove the provable leftovers (default: dry run; unrecognized files are never removed)",
    )
    clean.set_defaults(func=cmd_clean)

    rescue = sub.add_parser("rescue", help="inspect and safely restore preserved original files")
    rescue_sub = rescue.add_subparsers(dest="rescue_action", required=True)
    rescue_list = rescue_sub.add_parser("list", help="list manifest- and SHA-256-verified rescue files")
    rescue_list.add_argument("--workspace", default=".")
    rescue_list.set_defaults(func=cmd_rescue)
    rescue_restore = rescue_sub.add_parser("restore", help="restore a rescue file without overwriting its target")
    rescue_restore.add_argument("path", help="workspace-relative rescue path shown by rescue list")
    rescue_restore.add_argument("--workspace", default=".")
    rescue_restore.set_defaults(func=cmd_rescue)
    rescue_recover = rescue_sub.add_parser("recover", help="reconcile interrupted rescue restore staging files")
    rescue_recover.add_argument("--workspace", default=".")
    rescue_recover.set_defaults(func=cmd_rescue)
    rescue_clean = rescue_sub.add_parser("clean", help="preview or remove verified redundant rescue evidence")
    rescue_clean.add_argument("--workspace", default=".")
    rescue_clean.add_argument("--apply", action="store_true", help="perform verified cleanup (default: dry run)")
    rescue_clean.set_defaults(func=cmd_rescue)

    ev = sub.add_parser("eval", help="run the harness conformance / evaluation suite")
    ev.add_argument("--suite", default="all", choices=["all", "hcs", "e1e8"])
    ev.add_argument("--workspace", default=None, help="optional base dir to keep probe fixtures (default: temp)")
    ev.add_argument("--quiet", action="store_true")
    ev.set_defaults(func=cmd_eval)

    return parser


def main(argv: list[str] | None = None) -> int:
    _utf8_stdout()
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "task_flag", None):
        args.task = args.task_flag
    try:
        return int(args.func(args))
    except AgentError as exc:
        _print(f"[error] {exc.code}: {exc.message}", file=sys.stderr)
        return 4
    except BrokenPipeError:
        return 0
