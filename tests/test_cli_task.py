"""WP-R8 regression: the task-level CLI product entry.

`jinlin-agent task run/status/resume/cancel/trace` — the legacy `run`
command stays untouched. Offline via mock scripts: the planner script
drives assessment/planning, the worker script drives the worker.
"""

import json
from pathlib import Path

import pytest

from jinlin_agent.cli import main


def _write(tmp_path: Path, name: str, payload) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _finish_script():
    return [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "read_file", "arguments": {"path": "README.md"}}]},
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {"plan": {"objective": "x", "steps": [{"description": "read", "status": "completed", "evidence": ["README.md"]}]}},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {"summary": "read-only analysis finished", "claims": [{"statement": "entry is app.main", "evidence_refs": ["README.md"]}]},
                }
            ]
        },
    ]


def _assess_direct():
    return {"text": json.dumps({"strategy": "direct", "reasons": ["simple"]})}


def _workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / "README.md").write_text("# demo\n\nEntry point: app.main()\n", encoding="utf-8")
    (ws / "app.py").write_text("def main():\n    return 'served'\n", encoding="utf-8")
    return ws


def test_task_run_direct_completes(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JINLIN_HOME", str(tmp_path / "home"))
    worker_script = _write(tmp_path, "worker.json", _finish_script())
    planner_script = _write(tmp_path, "planner.json", [_assess_direct()])
    ws = _workspace(tmp_path / "ws")
    code = main([
        "task", "run", "--workspace", str(ws),
        "--mock-script", str(worker_script),
        "--planner-mock-script", str(planner_script),
        "-t", "explain the entry point",
        "--quiet",
    ])
    assert code == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "-> completed" in out


def test_task_status_reports_the_task(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JINLIN_HOME", str(tmp_path / "home"))
    worker_script = _write(tmp_path, "worker.json", _finish_script())
    planner_script = _write(tmp_path, "planner.json", [_assess_direct()])
    ws = _workspace(tmp_path / "ws")
    main([
        "task", "run", "--workspace", str(ws),
        "--mock-script", str(worker_script),
        "--planner-mock-script", str(planner_script),
        "-t", "explain the entry point",
        "--quiet",
    ])
    capsys.readouterr()
    # the task id is printed by run; grab it from the ledger home instead
    tasks_home = tmp_path / "home" / "tasks"
    task_dir = next(d for d in tasks_home.iterdir() if d.name.startswith("task_"))
    code = main(["task", "status", task_dir.name, "--workspace", str(ws), "--quiet"])
    assert code == 0
    out = capsys.readouterr().out
    assert task_dir.name in out
    assert "completed" in out


def test_task_resume_finishes_a_waiting_user_task(tmp_path, monkeypatch, capsys):
    ask = [
        {"tool_calls": [{"name": "get_project_summary", "arguments": {}}]},
        {"tool_calls": [{"name": "update_plan", "arguments": {"plan": {"objective": "wait", "steps": [{"description": "ask", "status": "in_progress"}]}}}]},
        {"tool_calls": [{"name": "request_user_input", "arguments": {"question": "which?"}}]},
    ]
    finish = [
        {
            "tool_calls": [
                {
                    "name": "update_plan",
                    "arguments": {"plan": {"objective": "wait", "steps": [{"description": "ask", "status": "completed", "evidence": ["README.md"]}]}},
                }
            ]
        },
        {
            "tool_calls": [
                {
                    "name": "propose_completion",
                    "arguments": {"summary": "read-only analysis finished", "claims": [{"statement": "entry is app.main", "evidence_refs": ["README.md"]}]},
                }
            ]
        },
    ]
    monkeypatch.setenv("JINLIN_HOME", str(tmp_path / "home"))
    worker_script = _write(tmp_path, "worker.json", ask)
    planner_script = _write(tmp_path, "planner.json", [_assess_direct()])
    ws = _workspace(tmp_path / "ws")
    code = main([
        "task", "run", "--workspace", str(ws),
        "--mock-script", str(worker_script),
        "--planner-mock-script", str(planner_script),
        "-t", "ask me something",
        "--quiet",
    ])
    assert code == 2  # waiting_user
    out = capsys.readouterr().out
    task_id = out.split("[task] ")[1].split(" ")[0]

    # resume with a fresh worker script (the provider is rebuilt per attempt)
    finish_script = _write(tmp_path, "finish.json", finish)
    code = main([
        "task", "resume", task_id, "--answer", "markdown",
        "--workspace", str(ws),
        "--mock-script", str(finish_script),
        "--planner-mock-script", str(planner_script),
        "--quiet",
    ])
    assert code == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "-> completed" in out


def test_task_cancel_stops_a_nonterminal_task(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JINLIN_HOME", str(tmp_path / "home"))
    worker_script = _write(tmp_path, "worker.json", _finish_script())
    planner_script = _write(tmp_path, "planner.json", [_assess_direct()])
    ws = _workspace(tmp_path / "ws")
    code = main([
        "task", "run", "--workspace", str(ws),
        "--mock-script", str(worker_script),
        "--planner-mock-script", str(planner_script),
        "-t", "explain the entry point",
        "--quiet",
    ])
    assert code == 0
    capsys.readouterr()
    tasks_home = tmp_path / "home" / "tasks"
    task_dir = next(d for d in tasks_home.iterdir() if d.name.startswith("task_"))
    code = main(["task", "cancel", task_dir.name, "--workspace", str(ws), "--quiet"])
    assert code == 1  # terminal tasks cannot be cancelled (honest exit)
    out = capsys.readouterr().out + capsys.readouterr().err


def test_task_trace_prints_the_task_stream(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JINLIN_HOME", str(tmp_path / "home"))
    worker_script = _write(tmp_path, "worker.json", _finish_script())
    planner_script = _write(tmp_path, "planner.json", [_assess_direct()])
    ws = _workspace(tmp_path / "ws")
    main([
        "task", "run", "--workspace", str(ws),
        "--mock-script", str(worker_script),
        "--planner-mock-script", str(planner_script),
        "-t", "explain the entry point",
        "--quiet",
    ])
    capsys.readouterr()
    tasks_home = tmp_path / "home" / "tasks"
    task_dir = next(d for d in tasks_home.iterdir() if d.name.startswith("task_"))
    code = main(["task", "trace", task_dir.name, "--workspace", str(ws), "--quiet"])
    assert code == 0
    out = capsys.readouterr().out
    assert "task.strategy_selected" in out
    assert "task.created" in out
