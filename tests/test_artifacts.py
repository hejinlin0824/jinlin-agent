"""Regression tests for the per-session artifact index."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jinlin_agent.artifacts import ArtifactStore


def test_index_uses_unique_same_directory_temp_and_persists(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    artifact = store.write("note", "hello")
    index_path = tmp_path / "artifacts" / "index.json"
    record = json.loads(index_path.read_text(encoding="utf-8"))[artifact.artifact_id]
    assert record["sha256"] == artifact.sha256
    assert not list(index_path.parent.glob(".index-*.tmp"))
    assert not (index_path.parent / "index.tmp").exists()


def test_index_replace_retries_transient_windows_access_denied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    real_replace = os.replace
    attempts = 0

    def flaky_replace(source, destination):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            exc = PermissionError("simulated transient Windows rename denial")
            exc.winerror = 5
            raise exc
        return real_replace(source, destination)

    monkeypatch.setattr("jinlin_agent.artifacts.os.replace", flaky_replace)
    artifact = store.write("note", "hello")
    assert attempts == 2
    index_path = tmp_path / "artifacts" / "index.json"
    assert artifact.artifact_id in json.loads(index_path.read_text(encoding="utf-8"))
    assert not list(index_path.parent.glob(".index-*.tmp"))


def test_persistent_index_replace_failure_preserves_previous_index_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.write("note", "first")
    index_path = tmp_path / "artifacts" / "index.json"
    original = index_path.read_bytes()

    def denied_replace(source, destination):
        exc = PermissionError("simulated persistent Windows rename denial")
        exc.winerror = 5
        raise exc

    monkeypatch.setattr("jinlin_agent.artifacts.os.replace", denied_replace)
    with pytest.raises(PermissionError):
        store.write("note", "second")
    assert index_path.read_bytes() == original
    assert first.artifact_id in json.loads(index_path.read_text(encoding="utf-8"))
    assert not list(index_path.parent.glob(".index-*.tmp"))
