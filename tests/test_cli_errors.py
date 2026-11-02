from __future__ import annotations

import socket

import pytest

from jinlin_agent.cli import main


@pytest.mark.parametrize("workspace_kind", ["missing", "file"])
def test_serve_reports_invalid_workspace_without_traceback(tmp_path, capsys, workspace_kind):
    if workspace_kind == "missing":
        workspace = tmp_path / "does-not-exist"
    else:
        workspace = tmp_path / "not-a-directory.txt"
        workspace.write_text("not a directory", encoding="utf-8")

    exit_code = main(["serve", "--workspace", str(workspace)])

    captured = capsys.readouterr()
    assert exit_code == 4
    assert "[error] CONFIG_INVALID:" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


def test_serve_reports_port_conflict_without_traceback(tmp_path, capsys):
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    occupied = socket.socket()
    occupied.bind(("127.0.0.1", 0))
    port = occupied.getsockname()[1]
    try:
        exit_code = main([
            "serve",
            "--workspace",
            str(workspace),
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ])
    finally:
        occupied.close()

    captured = capsys.readouterr()
    assert exit_code == 4
    assert "[error] SERVER_START_FAILED:" in captured.err
    assert "cannot bind workbench server" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("command", ["trace", "stats"])
def test_session_diagnostics_report_unknown_session_without_traceback(tmp_path, capsys, command):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    exit_code = main([
        command,
        "sess_20990101_000000_abcdef",
        "--workspace",
        str(workspace),
    ])

    captured = capsys.readouterr()
    assert exit_code == 4
    assert "[error]" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
