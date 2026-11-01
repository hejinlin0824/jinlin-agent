"""Controlled cleanup for jinlin trash files (rollback / txn leftovers).

Ownership policy (phase-2 principle: never overwrite, never delete by
unproven name, keep the scene and report): a leftover file is *removable*
only when its name matches the strict trash pattern AND the trash manifest
(``<workspace>/.jinlin/trash-manifest.jsonl``) records **its full
workspace-relative path** with a matching kind, original path and creation
fingerprint. The manifest entry is appended by the transaction that created
the file, so manifest membership is a transaction record — not a name-prefix
guess. Pattern-matched names without full manifest proof are reported and
never removed.

Deletion goes through a quarantine: the candidate is first atomically moved
to a unique quarantine name, the MOVED object's fingerprint is verified
against the manifest, and only a verified object is deleted. A mismatched
object is restored to its base path without ever overwriting and reported.
This removes the `stat → unlink` race as the final safety boundary: what is
deleted is always the object that was just verified, never whatever happens
to sit at the scanned path.

The manifest itself is rewritten atomically (temp file + fsync + replace):
a crash during a rewrite leaves the previous records intact instead of
silently dropping ownership. Manifest write failures never break the
transaction that produced the trash — the file then degrades to
"unrecognized" (report-only) and the degradation is reported to the caller.
Note: the directory-entry rename is not fsynced, so a power-loss exactly at
the replace is not covered — atomicity under normal operation is claimed,
not power-loss durability of the directory entry.

Cleanup runs under the workspace write lock (it cannot race an active jinlin
session), is repeatable, keeps manifest entries whose deletion failed, and
defaults to a dry run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from pathlib import Path, PurePosixPath
from typing import Any

from .domain import utcnow_iso
from .locks import SessionLock, SessionLockedError

TRASH_RE = re.compile(
    r"^\.(?P<orig>.+)\.jinlin-(?P<kind>rollback|txn)-(?P<tag>[0-9a-f]{8})(?P<q>\.q-[0-9a-f]{8})?$"
)
MANIFEST_REL = Path(".jinlin") / "trash-manifest.jsonl"
_SKIP_DIRS = {".git", ".jinlin"}


def _manifest_path(workspace: Path) -> Path:
    return Path(workspace) / MANIFEST_REL


def _relative_posix(path: Path, workspace: Path) -> str:
    try:
        return Path(path).relative_to(Path(workspace)).as_posix()
    except ValueError:
        return Path(path).as_posix()  # outside the workspace: keep an absolute posix id


def link_no_clobber(source: Path, dest: Path) -> bool:
    """Create ``dest`` as a no-clobber hard link to ``source``.

    The source name is deliberately retained. Unlinking it after a successful
    link would introduce a second path-replacement race: another process could
    replace the source name between ``link`` and ``unlink``, causing this
    helper to delete an object it never linked. Callers may later clean the
    retained quarantine name through the normal manifest/identity workflow.
    Returns False when ``dest`` is occupied or hard links are unsupported."""
    try:
        os.link(source, dest)
    except OSError:
        return False
    return True


def record_trash(workspace: Path, trash_path: Path, orig_rel: str, kind: str) -> bool:
    """Append a manifest entry for a trash/backup file this harness created.

    The entry binds the file's **workspace-relative path** (not just its
    basename) plus its creation fingerprint, so same-named files in different
    directories never share a proof and replacements at the same path are
    detectable. Best effort by design: the manifest is an ownership aid, and
    its failure must never break the transaction that produced the trash — the
    file then degrades to "unrecognized" (report-only) during cleanup and the
    caller is expected to surface the returned False.
    """
    rel = _relative_posix(trash_path, workspace)
    try:
        st = os.stat(trash_path)
        identity = {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        if kind == "rescue":
            identity["sha256"] = hashlib.sha256(Path(trash_path).read_bytes()).hexdigest()
    except OSError:
        identity = None  # no proof captured — the file degrades to report-only
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
                        "identity": identity,
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
    file (and therefore all ownership records) untouched. The directory-entry
    rename is not fsynced — power-loss durability of the rename itself is not
    claimed (see the module docstring)."""
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

    ``provable``: name matches the strict pattern (quarantine suffix
    included) AND the manifest records the base workspace-relative path with
    the same kind, original path and creation fingerprint. ``unrecognized``:
    pattern-matched names without full manifest proof — report-only.
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
            quarantined = match.group("q") is not None
            # the manifest records the pre-quarantine location: strip the
            # quarantine suffix to address the record
            base_rel = rel[: -len(match.group("q"))] if quarantined else rel
            entry = {
                "name": fname,
                "path": rel,
                "base_rel": base_rel,
                "abs": str(full),
                "orig": match.group("orig"),
                "kind": match.group("kind"),
                "quarantined": quarantined,
            }
            recorded = manifest.get(base_rel)
            # the trash NAME embeds only the original file's basename; the
            # manifest's orig may carry its directory (e.g. "sub/a.py"), so
            # correspondence is checked on the basename part. The recorded
            # creation fingerprint must also still match the file on disk —
            # a replacement at the same path is report-only.
            orig_ok = recorded is not None and Path(str(recorded.get("orig", ""))).name == match.group("orig")
            identity_ok = False
            if recorded is not None and isinstance(recorded.get("identity"), dict):
                try:
                    cur = os.stat(full)
                    ident = recorded["identity"]
                    identity_ok = (
                        cur.st_dev == ident.get("dev")
                        and cur.st_ino == ident.get("ino")
                        and cur.st_size == ident.get("size")
                        and cur.st_mtime_ns == ident.get("mtime_ns")
                    )
                except OSError:
                    identity_ok = False
            if recorded is not None and recorded.get("kind") == match.group("kind") and orig_ok and identity_ok:
                provable.append(entry)
            else:
                entry["reason"] = (
                    "manifest identity does not match the file on disk"
                    if (recorded is not None and orig_ok)
                    else "no manifest proof"
                )
                unrecognized.append(entry)
    provable.sort(key=lambda e: e["path"])
    unrecognized.sort(key=lambda e: e["path"])
    return provable, unrecognized


def clean_trash(workspace: Path, apply: bool = False) -> dict[str, Any]:
    """List (default, dry run) or remove provable jinlin trash.

    Runs under the workspace write lock so it cannot race an active jinlin
    session. Each candidate is first atomically moved OUT of its path into a
    unique quarantine name, the moved object is verified against the manifest
    fingerprint, and only a verified object is deleted. A mismatched object is
    restored to its base path without ever overwriting and reported as failed;
    per-file failures keep the manifest entry (repeatable runs converge) and
    are reported; unrecognized files are never touched.
    """
    workspace = Path(workspace)
    report: dict[str, Any] = {
        "workspace": str(workspace),
        "status": "dry-run",
        "removable": [],
        "unrecognized": [],
        "removed": [],
        "failed": [],
        "uncertain": [],
        "manifest_degraded": [],
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
        report["unrecognized"] = [(e["path"], e.get("reason", "")) for e in unrecognized]
        if not apply:
            return report
        report["status"] = "applied"
        for entry in provable:
            abs_path = Path(entry["abs"])
            base_abs = workspace / entry["base_rel"]
            if entry["quarantined"]:
                # already in quarantine from a previous run: verify + delete
                # without touching the scanned path again
                loc = abs_path
            else:
                # 1. atomically move the candidate OUT of its path into
                #    quarantine. Whatever sits at the path afterwards was put
                #    there by someone else and is never touched by this cleanup.
                quarantine = abs_path.with_name(f"{abs_path.name}.q-{uuid.uuid4().hex[:8]}")
                try:
                    os.replace(abs_path, quarantine)
                except OSError as exc:
                    report["failed"].append(
                        {"name": entry["name"], "path": entry["path"], "error": f"quarantine move failed: {exc}"}
                    )
                    continue
                loc = quarantine
            # 2. verify the MOVED object against the manifest fingerprint
            identity_ok = False
            try:
                cur = os.stat(loc)
                ident = _load_manifest(workspace).get(entry["base_rel"], {}).get("identity")
                if isinstance(ident, dict):
                    identity_ok = (
                        cur.st_dev == ident.get("dev")
                        and cur.st_ino == ident.get("ino")
                        and cur.st_size == ident.get("size")
                        and cur.st_mtime_ns == ident.get("mtime_ns")
                    )
            except OSError:
                identity_ok = False
            if not identity_ok:
                # 3. not provably ours: restore the moved object to its base
                #    path without ever overwriting, and report instead of
                #    deleting.
                if not link_no_clobber(loc, base_abs):
                    report["uncertain"].append(
                        {
                            "name": entry["name"],
                            "path": entry["path"],
                            "quarantine": str(loc),
                            "reason": "identity mismatch and the base path was claimed concurrently",
                        }
                    )
                    continue
                report["failed"].append(
                    {
                        "name": entry["name"],
                        "path": entry["path"],
                        "error": (
                            "ownership no longer provable (manifest or file identity mismatch); "
                            "object linked back without overwrite; quarantine evidence retained"
                        ),
                    }
                )
                if not forget_trash(workspace, base_abs):
                    report["manifest_degraded"].append(entry["path"])
                continue
            # 4. proven ours: delete the verified object (in quarantine)
            try:
                os.unlink(loc)
            except OSError as exc:
                report["failed"].append({"name": entry["name"], "path": entry["path"], "error": f"unlink failed: {exc}"})
                continue
            if not forget_trash(workspace, base_abs):
                report["manifest_degraded"].append(entry["path"])
            report["removed"].append(entry["path"])
        return report
    finally:
        lock.release()


RESCUE_RE = re.compile(r"^\.(?P<orig>.+)\.jinlin-rescue-(?P<tag>[0-9a-f]{8})$")


def list_rescue(workspace: Path) -> dict[str, Any]:
    """List only rescue files whose manifest identity and SHA-256 still match."""
    workspace = Path(workspace).resolve()
    report: dict[str, Any] = {"status": "ok", "rescued": [], "unrecognized": []}
    lock = SessionLock(workspace / ".jinlin" / "write.lock")
    try:
        lock.acquire()
    except SessionLockedError:
        return {"status": "locked", "rescued": [], "unrecognized": []}
    try:
        manifest = _load_manifest(workspace)
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
            for name in files:
                match = RESCUE_RE.match(name)
                if not match:
                    continue
                path = Path(root) / name
                rel = _relative_posix(path, workspace)
                entry = manifest.get(rel)
                ident = entry.get("identity") if entry else None
                try:
                    data = path.read_bytes()
                    st = path.stat()
                    valid = (
                        entry is not None
                        and entry.get("kind") == "rescue"
                        and Path(str(entry.get("orig", ""))).name == match.group("orig")
                        and isinstance(ident, dict)
                        and st.st_dev == ident.get("dev")
                        and st.st_ino == ident.get("ino")
                        and st.st_size == ident.get("size")
                        and st.st_mtime_ns == ident.get("mtime_ns")
                        and (ident.get("sha256") is None or hashlib.sha256(data).hexdigest() == ident.get("sha256"))
                    )
                except OSError:
                    valid = False
                    data = b""
                item = {"path": rel, "orig": str(entry.get("orig")) if entry else match.group("orig"),
                        "size": len(data), "sha256": hashlib.sha256(data).hexdigest() if valid else None,
                        "integrity": ("sha256" if isinstance(ident, dict) and ident.get("sha256") else "legacy-stat") if valid else "unverified"}
                (report["rescued"] if valid else report["unrecognized"]).append(item)
        report["rescued"].sort(key=lambda x: x["path"])
        report["unrecognized"].sort(key=lambda x: x["path"])
        return report
    finally:
        lock.release()


def restore_rescue(workspace: Path, rescue_rel: str) -> dict[str, Any]:
    """Restore a verified rescue copy with exclusive-create; never overwrite or consume source."""
    workspace = Path(workspace).resolve()
    lock = SessionLock(workspace / ".jinlin" / "write.lock")
    try:
        lock.acquire()
    except SessionLockedError:
        return {"status": "locked", "detail": "another jinlin session holds the workspace write lock"}
    try:
        listed = list_rescue_unlocked(workspace)
        item = next((x for x in listed["rescued"] if x["path"] == rescue_rel), None)
        if item is None:
            return {"status": "rejected", "detail": "rescue file is missing, unrecognized, or failed manifest/SHA-256 verification"}
        source = workspace / PurePosixPath(rescue_rel)
        manifest_entry = _load_manifest(workspace).get(rescue_rel)
        orig = str(manifest_entry.get("orig", "")) if manifest_entry else ""
        rel = PurePosixPath(orig)
        if not orig or rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
            return {"status": "rejected", "detail": "manifest target path is invalid"}
        dest = workspace.joinpath(*rel.parts)
        try:
            parent = dest.parent.resolve(strict=True)
            parent.relative_to(workspace)
        except (OSError, ValueError):
            return {"status": "rejected", "detail": "target parent is missing or outside the workspace"}
        if dest.is_symlink() or dest.exists():
            return {"status": "conflict", "detail": f"target already exists; rescue preserved at {rescue_rel}"}
        data = source.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != item["sha256"]:
            return {"status": "rejected", "detail": "rescue content changed during restore"}
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(dest, flags, 0o666)
        except FileExistsError:
            return {"status": "conflict", "detail": f"target was claimed concurrently; rescue preserved at {rescue_rel}"}
        write_error: OSError | None = None
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("write made no forward progress")
                view = view[count:]
            os.fsync(fd)
        except OSError as exc:
            write_error = exc
        finally:
            os.close(fd)
        if write_error is not None:
            # A partial target is deliberately retained as evidence. The
            # rescue source remains intact; a retry will refuse to overwrite.
            return {"status": "uncertain", "target": rel.as_posix(), "rescue": rescue_rel,
                    "detail": f"restore write/fsync failed ({write_error}); partial target retained, rescue preserved"}
        try:
            restored_data = dest.read_bytes()
        except OSError as exc:
            return {"status": "uncertain", "target": rel.as_posix(), "rescue": rescue_rel,
                    "detail": f"restored target could not be re-opened for verification ({exc}); rescue preserved"}
        if hashlib.sha256(restored_data).hexdigest() != digest:
            return {"status": "uncertain", "target": rel.as_posix(), "rescue": rescue_rel,
                    "detail": f"restored target failed SHA-256 verification; rescue preserved at {rescue_rel}"}
        return {"status": "restored", "target": rel.as_posix(), "rescue": rescue_rel, "sha256": digest,
                "detail": "restore verified; rescue source intentionally retained"}
    finally:
        lock.release()


def list_rescue_unlocked(workspace: Path) -> dict[str, Any]:
    """Internal scan for callers already holding the workspace write lock."""
    manifest = _load_manifest(workspace)
    rescued, unrecognized = [], []
    for root, dirs, files in os.walk(workspace):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for name in files:
            match = RESCUE_RE.match(name)
            if not match:
                continue
            path = Path(root) / name
            rel = _relative_posix(path, workspace)
            entry = manifest.get(rel)
            ident = entry.get("identity") if entry else None
            try:
                data = path.read_bytes()
                st = path.stat()
                valid = (entry is not None and entry.get("kind") == "rescue"
                         and Path(str(entry.get("orig", ""))).name == match.group("orig")
                         and isinstance(ident, dict) and st.st_dev == ident.get("dev")
                         and st.st_ino == ident.get("ino") and st.st_size == ident.get("size")
                         and st.st_mtime_ns == ident.get("mtime_ns")
                         and (ident.get("sha256") is None or hashlib.sha256(data).hexdigest() == ident.get("sha256")))
            except OSError:
                valid, data = False, b""
            item = {"path": rel, "orig": str(entry.get("orig")) if entry else match.group("orig"),
                    "size": len(data), "sha256": hashlib.sha256(data).hexdigest() if valid else None}
            (rescued if valid else unrecognized).append(item)
    rescued.sort(key=lambda x: x["path"])
    unrecognized.sort(key=lambda x: x["path"])
    return {"rescued": rescued, "unrecognized": unrecognized}
