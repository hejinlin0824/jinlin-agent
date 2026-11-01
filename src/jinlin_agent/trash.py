"""Controlled cleanup for jinlin trash files (rollback / txn leftovers).

Ownership policy (phase-2 principle: never overwrite, never delete by
unproven name, keep the scene and report): a leftover file is *removable*
only when its name matches the strict trash pattern AND the trash manifest
(``<workspace>/.jinlin/trash-manifest.jsonl``) records it. The manifest entry
is appended by the transaction that created the file, so manifest membership
is a transaction record — not just a name-prefix guess. Pattern-matched
names without manifest proof are reported and never removed.

Cleanup runs under the workspace write lock (it cannot race an active jinlin
session), is repeatable, keeps manifest entries whose deletion failed, and
defaults to a dry run. Best-effort failures of the manifest itself never
break the transactions that produced the trash — they only downgrade files
to "unrecognized" (report-only).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .domain import utcnow_iso
from .locks import SessionLock, SessionLockedError

TRASH_RE = re.compile(r"^\.(?P<orig>.+)\.jinlin-(?P<kind>rollback|txn)-(?P<tag>[0-9a-f]{8})$")
MANIFEST_REL = Path(".jinlin") / "trash-manifest.jsonl"
_SKIP_DIRS = {".git", ".jinlin"}


def _manifest_path(workspace: Path) -> Path:
    return Path(workspace) / MANIFEST_REL


def record_trash(workspace: Path, name: str, orig_rel: str, kind: str) -> None:
    """Append a manifest entry for a trash/backup file this harness created.

    Best effort by design: the manifest is an ownership aid, and its failure
    must never break the transaction that produced the trash — the file then
    simply degrades to "unrecognized" (report-only) during cleanup.
    """
    try:
        path = _manifest_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {"name": name, "orig": orig_rel, "kind": kind, "ts": utcnow_iso()},
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.flush()
    except OSError:
        pass


def forget_trash(workspace: Path, name: str) -> None:
    """Drop one manifest entry (after its file was successfully removed)."""
    entries = _load_manifest(workspace)
    if name not in entries:
        return
    entries.pop(name)
    try:
        with _manifest_path(workspace).open("w", encoding="utf-8") as fh:
            for entry in entries.values():
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
    except OSError:
        pass


def _load_manifest(workspace: Path) -> dict[str, dict[str, Any]]:
    """Parse the manifest; a torn trailing line (crash during append) is
    skipped — an unreadable entry is not an ownership proof."""
    entries: dict[str, dict[str, Any]] = {}
    try:
        data = _manifest_path(workspace).read_bytes()
    except OSError:
        return entries
    for line in data.split(b"\n"):
        if not line.strip():
            continue
        try:
            entry = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            entries[entry["name"]] = entry
    return entries


def scan_trash(workspace: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(provable, unrecognized)`` trash candidates.

    ``provable``: name matches the strict pattern AND the manifest records it
    with the same kind. ``unrecognized``: pattern-matched names without
    manifest proof — report-only.
    """
    workspace = Path(workspace)
    manifest = _load_manifest(workspace)
    provable: list[dict[str, Any]] = []
    unrecognized: list[dict[str, Any]] = []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            match = TRASH_RE.match(fname)
            if not match:
                continue
            full = Path(root) / fname
            entry = {
                "name": fname,
                "path": str(full),
                "orig": match.group("orig"),
                "kind": match.group("kind"),
            }
            recorded = manifest.get(fname)
            if recorded is not None and recorded.get("kind") == match.group("kind"):
                provable.append(entry)
            else:
                unrecognized.append(entry)
    provable.sort(key=lambda e: e["name"])
    unrecognized.sort(key=lambda e: e["name"])
    return provable, unrecognized


def clean_trash(workspace: Path, apply: bool = False) -> dict[str, Any]:
    """List (default, dry run) or remove provable jinlin trash.

    Runs under the workspace write lock so it cannot race an active jinlin
    session. Per-file failures keep the manifest entry (repeatable runs
    converge) and are reported; unrecognized files are never touched.
    """
    workspace = Path(workspace)
    report: dict[str, Any] = {
        "workspace": str(workspace),
        "status": "dry-run",
        "removable": [],
        "unrecognized": [],
        "removed": [],
        "failed": [],
    }
    lock = SessionLock(workspace / ".jinlin" / "write.lock")
    try:
        lock.acquire()
    except SessionLockedError:
        report["status"] = "locked"
        report["detail"] = "another jinlin session holds the workspace write lock"
        return report
    try:
        provable, unrecognized = scan_trash(workspace)
        report["removable"] = [e["name"] for e in provable]
        report["unrecognized"] = [e["name"] for e in unrecognized]
        if not apply:
            return report
        report["status"] = "applied"
        for entry in provable:
            try:
                os.unlink(entry["path"])
                forget_trash(workspace, entry["name"])
                report["removed"].append(entry["name"])
            except OSError as exc:
                report["failed"].append({"name": entry["name"], "error": str(exc)})
        return report
    finally:
        lock.release()
