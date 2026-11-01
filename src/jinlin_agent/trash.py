"""Controlled cleanup for jinlin trash files (rollback / txn leftovers).

Ownership policy (phase-2 principle: never overwrite, never delete by
unproven name, keep the scene and report): a leftover file is *removable*
only when its name matches the strict trash pattern AND the trash manifest
(``<workspace>/.jinlin/trash-manifest.jsonl``) records **its full
workspace-relative path** with a matching kind and original path. The
manifest entry is appended by the transaction that created the file, so
manifest membership is a transaction record — not a name-prefix guess: two
files sharing a basename in different directories never share a proof, and
each removal is re-verified against a freshly loaded manifest before the
unlink.

The manifest itself is rewritten atomically (temp file + fsync + replace):
a crash during a rewrite leaves the previous records intact instead of
silently dropping ownership. Manifest write failures never break the
transaction that produced the trash — the file then degrades to
"unrecognized" (report-only) and the degradation is reported to the caller.

Cleanup runs under the workspace write lock (it cannot race an active jinlin
session), is repeatable, keeps manifest entries whose deletion failed, and
defaults to a dry run.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any

from .domain import utcnow_iso
from .locks import SessionLock, SessionLockedError

TRASH_RE = re.compile(r"^\.(?P<orig>.+)\.jinlin-(?P<kind>rollback|txn)-(?P<tag>[0-9a-f]{8})$")
MANIFEST_REL = Path(".jinlin") / "trash-manifest.jsonl"
_SKIP_DIRS = {".git", ".jinlin"}


def _manifest_path(workspace: Path) -> Path:
    return Path(workspace) / MANIFEST_REL


def _relative_posix(path: Path, workspace: Path) -> str:
    try:
        return Path(path).relative_to(Path(workspace)).as_posix()
    except ValueError:
        return Path(path).as_posix()  # outside the workspace: keep an absolute posix id


def record_trash(workspace: Path, trash_path: Path, orig_rel: str, kind: str) -> bool:
    """Append a manifest entry for a trash/backup file this harness created.

    The entry binds the file's **workspace-relative path** (not just its
    basename), so same-named files in different directories never share a
    proof. Best effort by design: the manifest is an ownership aid, and its
    failure must never break the transaction that produced the trash — the
    file then degrades to "unrecognized" (report-only) during cleanup and the
    caller is expected to surface the returned False.
    """
    rel = _relative_posix(trash_path, workspace)
    try:
        path = _manifest_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "path": rel,
                        "name": trash_path.name,
                        "orig": orig_rel,
                        "kind": kind,
                        "ts": utcnow_iso(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            fh.flush()
        return True
    except OSError:
        return False


def _load_manifest(workspace: Path) -> dict[str, dict[str, Any]]:
    """Parse the manifest keyed by the recorded workspace-relative path; a
    torn trailing line (crash during append) is skipped — an unreadable entry
    is not an ownership proof."""
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
        if isinstance(entry, dict) and isinstance(entry.get("path"), str):
            entries[entry["path"]] = entry
    return entries


def _write_manifest_atomic(workspace: Path, entries: dict[str, dict[str, Any]]) -> bool:
    """Rewrite the whole manifest atomically; a failure leaves the previous
    file (and therefore all ownership records) untouched."""
    path = _manifest_path(workspace)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            for entry in entries.values():
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def forget_trash(workspace: Path, trash_path: Path) -> bool:
    """Drop one manifest entry, addressed by the file's path — never by
    basename, so same-named entries in different directories stay
    independent."""
    rel = _relative_posix(trash_path, workspace)
    entries = _load_manifest(workspace)
    if rel not in entries:
        return True  # nothing recorded — nothing to forget
    entries.pop(rel)
    return _write_manifest_atomic(workspace, entries)


def scan_trash(workspace: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return ``(provable, unrecognized)`` trash candidates.

    ``provable``: name matches the strict pattern AND the manifest records the
    same workspace-relative path with the same kind and original path.
    ``unrecognized``: pattern-matched names without full manifest proof —
    report-only.
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
            rel = _relative_posix(full, workspace)
            entry = {
                "name": fname,
                "path": rel,
                "abs": str(full),
                "orig": match.group("orig"),
                "kind": match.group("kind"),
            }
            recorded = manifest.get(rel)
            # the trash NAME embeds only the original file's basename; the
            # manifest's orig may carry its directory (e.g. "sub/a.py"), so
            # correspondence is checked on the basename part
            orig_ok = recorded is not None and Path(str(recorded.get("orig", ""))).name == match.group("orig")
            if recorded is not None and recorded.get("kind") == match.group("kind") and orig_ok:
                provable.append(entry)
            else:
                unrecognized.append(entry)
    provable.sort(key=lambda e: e["path"])
    unrecognized.sort(key=lambda e: e["path"])
    return provable, unrecognized


def clean_trash(workspace: Path, apply: bool = False) -> dict[str, Any]:
    """List (default, dry run) or remove provable jinlin trash.

    Runs under the workspace write lock so it cannot race an active jinlin
    session. Each removal re-verifies ownership against a freshly loaded
    manifest immediately before the unlink; per-file failures keep the
    manifest entry (repeatable runs converge) and are reported; unrecognized
    files are never touched.
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
        report["removable"] = [e["path"] for e in provable]
        report["unrecognized"] = [e["path"] for e in unrecognized]
        if not apply:
            return report
        report["status"] = "applied"
        for entry in provable:
            manifest = _load_manifest(workspace)  # fresh proof, per file
            recorded = manifest.get(entry["path"])
            orig_ok = recorded is not None and Path(str(recorded.get("orig", ""))).name == entry["orig"]
            if recorded is None or recorded.get("kind") != entry["kind"] or not orig_ok:
                report["failed"].append(
                    {"name": entry["name"], "path": entry["path"], "error": "manifest no longer proves ownership"}
                )
                continue
            try:
                os.unlink(entry["abs"])
                forget_trash(workspace, Path(entry["abs"]))
                report["removed"].append(entry["path"])
            except OSError as exc:
                report["failed"].append({"name": entry["name"], "path": entry["path"], "error": str(exc)})
        return report
    finally:
        lock.release()
