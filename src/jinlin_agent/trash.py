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

The manifest is appended with a complete JSONL record, file-fsynced,
and its parent directory is fsynced when the manifest is first created.
Rewrites use an exclusive temporary file, fsync its contents, replace the
manifest, and fsync the containing directory where supported. A malformed or
torn existing manifest blocks append/rewrite rather than allowing a later
write to hide or concatenate damaged history. On platforms where directory
fsync is unavailable, notably portable Windows Python, directory-entry
durability is explicitly best-effort rather than guaranteed.

Cleanup runs under the workspace write lock (it cannot race an active jinlin
session), is repeatable, keeps manifest entries whose deletion failed, and
defaults to a dry run.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
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


AUDIT_REL = Path(".jinlin") / "rescue-audit.jsonl"
AUDIT_LOCK_REL = Path(".jinlin") / "rescue-audit.lock"


def _audit_path(workspace: Path) -> Path:
    return Path(workspace) / AUDIT_REL


def _read_audit(workspace: Path) -> dict[str, Any]:
    """Read and verify the append-only, hash-chained rescue audit log."""
    path = _audit_path(workspace)
    events: list[dict[str, Any]] = []
    issues: list[dict[str, Any]] = []
    previous = "0" * 64
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raw = b""
    except OSError as exc:
        return {"events": [], "issues": [{"line": 0, "reason": f"read failed: {exc}"}], "last_hash": previous}
    seen_event_ids: set[str] = set()
    seen_version_2 = False
    for number, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line.decode("utf-8"))
            if not isinstance(event, dict):
                raise ValueError("event is not an object")
            version = event.get("version")
            if type(version) is not int or version not in (1, 2):
                raise ValueError(f"unsupported audit event version: {version!r}")
            txid = event.get("txid")
            if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-f]{24,32}", txid):
                raise ValueError("transaction id is missing or malformed")
            if not isinstance(event.get("action"), str) or not event.get("action"):
                raise ValueError("event action is missing or malformed")
            if not isinstance(event.get("status"), str) or not event.get("status"):
                raise ValueError("event status is missing or malformed")
            if not isinstance(event.get("ts"), str) or not event.get("ts"):
                raise ValueError("event timestamp is missing or malformed")
            if version == 2:
                seen_version_2 = True
                expected_seq = len(events) + 1
                if type(event.get("seq")) is not int or event.get("seq") != expected_seq:
                    raise ValueError(f"event sequence is not contiguous (expected {expected_seq})")
                event_id = event.get("event_id")
                if not isinstance(event_id, str) or not re.fullmatch(r"[0-9a-f]{32}", event_id):
                    raise ValueError("event id is missing or malformed")
                if event_id in seen_event_ids:
                    raise ValueError("duplicate event id")
                seen_event_ids.add(event_id)
            elif seen_version_2:
                raise ValueError("legacy version-1 event cannot follow version-2 events")
            supplied = event.get("event_hash")
            if not isinstance(supplied, str) or not re.fullmatch(r"[0-9a-f]{64}", supplied):
                raise ValueError("event hash is missing or malformed")
            body = {key: value for key, value in event.items() if key != "event_hash"}
            expected = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
            if event.get("prev_hash") != previous:
                raise ValueError("previous hash does not match")
            if supplied != expected:
                raise ValueError("event hash does not match")
            previous = supplied
            events.append(event)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            issues.append({"line": number, "reason": str(exc)})
            # A broken chain is never silently repaired or extended.
            break
    return {"events": events, "issues": issues, "last_hash": previous}


def _read_audit_consistent(workspace: Path) -> dict[str, Any]:
    """Read an audit snapshot while excluding an in-progress append."""
    lock = SessionLock(Path(workspace) / AUDIT_LOCK_REL)
    try:
        lock.acquire()
    except SessionLockedError:
        return {
            "events": [],
            "issues": [{"line": 0, "reason": "audit log is locked by another writer; retry inspection"}],
            "last_hash": "0" * 64,
        }
    try:
        return _read_audit(workspace)
    finally:
        lock.release()


def _append_audit_event(workspace: Path, action: str, status: str, **fields: Any) -> tuple[bool, str | None]:
    """Append one fsynced event under a dedicated lock; never rewrite damaged logs."""
    workspace = Path(workspace)
    lock = SessionLock(workspace / AUDIT_LOCK_REL)
    try:
        lock.acquire()
    except SessionLockedError:
        return False, "audit log is locked by another writer"
    try:
        audit = _read_audit(workspace)
        if audit["issues"]:
            return False, "audit log has integrity issues; refusing to append"
        txid = fields.pop("txid", None)
        if txid in (None, ""):
            # Compatibility path for callers and old manifest entries that
            # predate persistent transaction identifiers.
            identity = "|".join(str(fields.get(key, "")) for key in ("rescue", "target", "sha256"))
            txid = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        elif not isinstance(txid, str) or not re.fullmatch(r"[0-9a-f]{24,32}", txid):
            return False, "transaction id is malformed; refusing to append an uncorrelatable event"
        body = {
            "version": 2, "seq": len(audit["events"]) + 1, "event_id": uuid.uuid4().hex,
            "ts": utcnow_iso(), "txid": txid,
            "action": action, "status": status, "prev_hash": audit["last_hash"],
            **fields,
        }
        body["event_hash"] = hashlib.sha256(
            json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        encoded = (json.dumps(body, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
        path = _audit_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("audit log is not a regular file")
            view = memoryview(encoded)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("audit append made no forward progress")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            directory_sync = _fsync_directory(path.parent)
            if directory_sync["supported"] and not directory_sync["synced"]:
                return False, "audit file was appended and fsynced but its directory entry could not be synced: " + directory_sync["detail"]
        return True, None
    except OSError as exc:
        return False, f"audit append failed: {exc}"
    finally:
        lock.release()


def _audit_result(workspace: Path, action: str, result: dict[str, Any], **fields: Any) -> dict[str, Any]:
    status = str(result.get("status", "unknown"))
    event_fields = dict(fields)
    for key in ("rescue", "target", "stage", "sha256", "detail"):
        if result.get(key) is not None:
            event_fields.setdefault(key, result[key])
    event_fields.setdefault("txid", result.get("txid"))
    if not event_fields.get("txid") and event_fields.get("rescue"):
        entry = _load_manifest(workspace).get(str(event_fields["rescue"]), {})
        event_fields["txid"] = entry.get("transaction_id")
    ok, warning = _append_audit_event(workspace, action, status, **event_fields)
    if not ok:
        result.setdefault("audit_warnings", []).append(warning or "audit append failed")
    return result


def _transaction_states(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project ordered audit events into transaction state and consistency evidence.

    Dry-run observations remain in the audit chain but do not create or advance
    a transaction. Legacy version-1 events remain readable; version-2 events
    carry a persistent transaction id and a globally ordered event sequence.
    """
    allowed = {
        "CREATED": {"STAGED", "PUBLISHED", "CONFLICT", "UNCERTAIN", "ORPHANED", "CLEANUP_BLOCKED"},
        "STAGED": {"STAGED", "PUBLISHED", "CONFLICT", "UNCERTAIN", "CLEANUP_BLOCKED"},
        "PUBLISHED": {"PUBLISHED", "CONFLICT", "UNCERTAIN", "CLEANED", "CLEANUP_BLOCKED"},
        "CONFLICT": {"CONFLICT", "PUBLISHED", "UNCERTAIN", "CLEANUP_BLOCKED"},
        "UNCERTAIN": {"UNCERTAIN", "STAGED", "PUBLISHED", "CONFLICT", "CLEANUP_BLOCKED"},
        "ORPHANED": {"ORPHANED", "UNCERTAIN", "PUBLISHED"},
        "CLEANUP_BLOCKED": {"CLEANUP_BLOCKED", "CLEANED", "PUBLISHED", "CONFLICT", "UNCERTAIN"},
        "CLEANED": {"CLEANED"},
        "TRANSITION_INVALID": {"TRANSITION_INVALID"},
    }
    states: dict[str, dict[str, Any]] = {}
    for event in events:
        action = str(event.get("action", ""))
        status = str(event.get("status", "unknown"))
        if status in ("dry-run", "preview"):
            continue
        txid = str(event.get("txid", ""))
        if not txid:
            continue
        item = states.setdefault(txid, {
            "txid": txid, "state": "CREATED", "history": [],
            "transition_issues": [], "identity_issues": [],
            "identity": {},
        })
        for key in ("rescue", "target", "sha256"):
            value = event.get(key)
            if value in (None, ""):
                continue
            previous_value = item["identity"].get(key)
            if previous_value not in (None, value):
                item["identity_issues"].append({
                    "field": key, "previous": previous_value, "observed": value,
                    "event_id": event.get("event_id"), "seq": event.get("seq"),
                    "reason": "one transaction id was reused for different resource identity",
                })
            else:
                item["identity"][key] = value

        if action == "clean" and status == "removed":
            next_state = "CLEANED"
        elif action in ("restore", "recover", "quarantine-recover") and status in ("restored", "already-published", "already-restored"):
            next_state = "PUBLISHED"
        elif action in ("restore", "recover", "quarantine-recover") and status in ("staged", "incomplete"):
            next_state = "STAGED"
        elif status in ("conflict", "ambiguous"):
            next_state = "CONFLICT"
        elif status in ("rejected", "uncertain", "unrecognized", "published-not-durable", "blocked"):
            next_state = "UNCERTAIN"
        elif status == "orphaned":
            next_state = "ORPHANED"
        elif action == "clean" and status == "cleanup-blocked":
            next_state = "CLEANUP_BLOCKED"
        else:
            next_state = None

        previous_state = item["state"]
        if next_state is not None and next_state != previous_state:
            if next_state not in allowed.get(previous_state, set()):
                item["transition_issues"].append({
                    "from": previous_state, "to": next_state, "action": action, "status": status,
                    "event_id": event.get("event_id"), "seq": event.get("seq"),
                    "reason": "transition is not allowed by the Rescue lifecycle state machine",
                })
                item["state"] = "TRANSITION_INVALID"
            elif previous_state != "TRANSITION_INVALID":
                item["state"] = next_state
        if item["identity_issues"] and item["state"] != "TRANSITION_INVALID":
            item["transition_issues"].append({
                "from": item["state"], "to": "TRANSITION_INVALID", "action": action, "status": status,
                "reason": "transaction resource identity changed within one transaction",
            })
            item["state"] = "TRANSITION_INVALID"
        item["last_action"] = action
        item["last_status"] = status
        item["last_ts"] = event.get("ts")
        item["event_count"] = item.get("event_count", 0) + 1
        item["first_seq"] = item.get("first_seq", event.get("seq"))
        item["last_seq"] = event.get("seq", item.get("last_seq"))
        item["history"].append({
            "action": action, "status": status, "ts": event.get("ts"),
            "event_id": event.get("event_id"), "seq": event.get("seq"),
            "state": item["state"],
        })
    return sorted(states.values(), key=lambda item: (item.get("last_seq") or 0, item.get("last_ts") or ""))


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
    """Append a durable ownership record; never append behind a torn manifest."""
    rel = _relative_posix(trash_path, workspace)
    try:
        st = os.stat(trash_path, follow_symlinks=False)
        if not stat.S_ISREG(st.st_mode):
            return False
        identity = {"dev": st.st_dev, "ino": st.st_ino, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
        if kind == "rescue":
            identity["sha256"] = hashlib.sha256(Path(trash_path).read_bytes()).hexdigest()
        path = _manifest_path(workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            existing = path.read_bytes()
        except FileNotFoundError:
            existing = b""
        if existing and not existing.endswith(b"\n"):
            return False
        for line in existing.splitlines():
            if not line.strip():
                continue
            parsed = json.loads(line.decode("utf-8"))
            if not isinstance(parsed, dict) or not isinstance(parsed.get("path"), str):
                return False
        record = {
            "path": rel, "name": trash_path.name, "orig": orig_rel, "kind": kind,
            "identity": identity, "transaction_id": uuid.uuid4().hex, "ts": utcnow_iso(),
        }
        encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
        created = False
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            try:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o600)
                created = True
            except FileExistsError:
                fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError("manifest is not a regular file")
            view = memoryview(encoded)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("manifest append made no forward progress")
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            directory_sync = _fsync_directory(path.parent)
            if directory_sync["supported"] and not directory_sync["synced"]:
                return False
        return True
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError):
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
    """Durably replace the manifest without discarding damaged ownership rows."""
    path = _manifest_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = path.read_bytes()
    except FileNotFoundError:
        existing = b""
    except OSError:
        return False
    if existing:
        if not existing.endswith(b"\n"):
            return False
        try:
            for line in existing.splitlines():
                if not line.strip():
                    continue
                parsed = json.loads(line.decode("utf-8"))
                if not isinstance(parsed, dict) or not isinstance(parsed.get("path"), str):
                    return False
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            return False

    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = None
    try:
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        for entry in entries.values():
            view = memoryview((json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("manifest write made no forward progress")
                view = view[count:]
        os.fsync(fd)
        os.close(fd)
        fd = None
        os.replace(tmp, path)
        directory_sync = _fsync_directory(path.parent)
        return not directory_sync["supported"] or directory_sync["synced"]
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass


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
RESTORE_STAGE_RE = re.compile(r"^\.(?P<target>.+)\.jinlin-restore-(?P<digest>[0-9a-f]{16})$")
CLEAN_QUARANTINE_RE = re.compile(r"^\..+\.jinlin-clean-[0-9a-f]{8}$")

def _inspect_clean_quarantines(workspace: Path, manifest: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify interrupted cleanup quarantines without changing the filesystem."""
    rescue_expected: dict[str, dict[str, Any]] = {}
    stage_expected: dict[str, dict[str, Any]] = {}
    for rescue_rel, entry in manifest.items():
        if entry.get("kind") != "rescue":
            continue
        identity = entry.get("identity")
        if not isinstance(identity, dict):
            continue
        digest = identity.get("sha256")
        if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest):
            rescue_expected[rescue_rel] = {
                "sha256": digest,
                "identity": entry.get("quarantine_identity", identity),
                "candidate_identity": identity,
                "transaction_id": entry.get("transaction_id"),
                "rescue": rescue_rel,
                "target": entry.get("orig"),
            }
            target, error = _safe_rescue_target(workspace, str(entry.get("orig", "")))
            if not error and target is not None:
                stage = _restore_stage_path(target, digest)
                stage_expected[_relative_posix(stage, workspace)] = {
                    "sha256": digest, "rescue": rescue_rel, "target": entry.get("orig"),
                    "transaction_id": entry.get("transaction_id")
                }
        else:
            rescue_expected[rescue_rel] = {"sha256": None, "identity": identity}

    items: list[dict[str, Any]] = []
    by_candidate: dict[str, list[dict[str, Any]]] = {}
    suffix_re = re.compile(r"^(?P<base>.+)\.jinlin-clean-[0-9a-f]{8}$")
    for root, dirs, files in os.walk(workspace, followlinks=False):
        dirs[:] = [name for name in dirs if name not in _SKIP_DIRS]
        for name in list(files) + list(dirs):
            match = suffix_re.match(name[1:]) if name.startswith(".") else None
            if not match:
                continue
            path = Path(root) / name
            candidate = path.with_name(match.group("base"))
            quarantine_rel = _relative_posix(path, workspace)
            candidate_rel = _relative_posix(candidate, workspace)
            kind = "symlink" if path.is_symlink() else ("directory" if path.is_dir() else "file")
            item: dict[str, Any] = {"path": quarantine_rel, "kind": kind, "candidate": candidate_rel, "state": "CLEANUP_INTERRUPTED"}
            expected = rescue_expected.get(candidate_rel)
            candidate_kind = "rescue"
            if expected is None:
                expected = stage_expected.get(candidate_rel)
                candidate_kind = "stage"
            item["candidate_kind"] = candidate_kind if expected is not None else "unknown"
            if expected is not None:
                item["transaction_id"] = expected.get("transaction_id")
                item["rescue"] = expected.get("rescue", candidate_rel if candidate_kind == "rescue" else None)
                item["target"] = expected.get("target")
            if expected is None:
                item["classification"] = "unrecognized"
                item["reason"] = "no manifest-backed Rescue or deterministic restore stage matches the former path"
            elif kind != 'file':
                item["classification"] = "identity-mismatch"
                item["reason"] = "quarantine is not a regular non-symlink file"
            else:
                try:
                    st = os.stat(path, follow_symlinks=False)
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    item["sha256"] = digest
                    if expected.get("sha256") is None:
                        item["classification"] = "legacy-unverified"
                        item["reason"] = "legacy manifest has no SHA-256 proof for the Rescue payload"
                    elif digest != expected["sha256"]:
                        item["classification"] = "identity-mismatch"
                        item["reason"] = "quarantine bytes do not match the manifest-bound SHA-256"
                    elif candidate_kind == "rescue":
                        ident = expected.get("identity") or {}
                        actual = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
                        recorded = (ident.get("dev"), ident.get("ino"), ident.get("size"), ident.get("mtime_ns"))
                        if actual != recorded:
                            item["classification"] = "identity-mismatch"
                            item["reason"] = "quarantine file identity differs from the recorded Rescue identity"
                        else:
                            item["classification"] = "safe-to-restore"
                    else:
                        item["classification"] = "safe-to-restore"
                except OSError as exc:
                    item["classification"] = "identity-mismatch"
                    item["reason"] = f"quarantine cannot be read and verified: {exc}"
            if candidate.exists() or candidate.is_symlink():
                if item["classification"] == "safe-to-restore":
                    if not candidate.is_symlink():
                        try:
                            candidate_st = os.stat(candidate, follow_symlinks=False)
                            candidate_identity = (candidate_st.st_dev, candidate_st.st_ino, candidate_st.st_size, candidate_st.st_mtime_ns)
                            recorded_candidate = expected.get("candidate_identity") if candidate_kind == "rescue" else None
                            identity_matches = (
                                isinstance(recorded_candidate, dict)
                                and candidate_identity == (
                                    recorded_candidate.get("dev"), recorded_candidate.get("ino"),
                                    recorded_candidate.get("size"), recorded_candidate.get("mtime_ns")
                                )
                            )
                            recovery_stage = _quarantine_restore_stage_path(candidate, str(expected.get("sha256", "")))
                            stage_matches = False
                            if candidate_kind == "rescue" and (recovery_stage.exists() or recovery_stage.is_symlink()) and not recovery_stage.is_symlink():
                                try:
                                    stage_st = os.stat(recovery_stage, follow_symlinks=False)
                                    stage_matches = _same_file_identity(candidate_st, stage_st)
                                except OSError:
                                    stage_matches = False
                            rescue_already_proven = candidate_kind != "rescue" or identity_matches or stage_matches
                            if (stat.S_ISREG(candidate_st.st_mode)
                                    and hashlib.sha256(candidate.read_bytes()).hexdigest() == expected.get("sha256")
                                    and rescue_already_proven):
                                item["classification"] = "already-restored"
                                item["reason"] = "former pathname already contains the verified bytes and its recovery identity is consistent"
                            else:
                                item["classification"] = "target-conflict"
                                item["reason"] = "former pathname is occupied by unproven or different content"
                        except OSError as exc:
                            item["classification"] = "target-conflict"
                            item["reason"] = f"former pathname is occupied and cannot be verified: {exc}"
                    else:
                        item["classification"] = "target-conflict"
                        item["reason"] = "former pathname is a symlink; no-clobber restoration is not possible"
            by_candidate.setdefault(candidate_rel, []).append(item)
            items.append(item)
    for claims in by_candidate.values():
        if len(claims) > 1:
            for item in claims:
                if item["classification"] in ("safe-to-restore", "already-restored"):
                    item["classification"] = "ambiguous"
                    item["reason"] = "multiple quarantine objects claim the same former pathname; automatic recovery is disabled"
    items.sort(key=lambda item: item["path"])
    return items



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


def _restore_stage_path(dest: Path, digest: str) -> Path:
    """Deterministic sibling staging path, discoverable after process interruption."""
    return dest.with_name(f".{dest.name}.jinlin-restore-{digest[:16]}")


def _safe_rescue_target(workspace: Path, orig: str) -> tuple[Path | None, str | None]:
    # Manifest paths are POSIX relative paths on every platform. Reject
    # backslashes and drive/ADS syntax rather than interpreting them differently.
    if (not orig or chr(92) in orig or ":" in orig or chr(0) in orig
            or orig.startswith("/") or orig.startswith("//")):
        return None, "manifest target path is invalid"
    rel = PurePosixPath(orig)
    if rel.is_absolute() or any(part in ("", ".", "..") for part in rel.parts):
        return None, "manifest target path is invalid"
    dest = workspace.joinpath(*rel.parts)
    try:
        parent = dest.parent.resolve(strict=True)
        parent.relative_to(workspace)
    except (OSError, ValueError):
        return None, "target parent is missing or outside the workspace"
    return dest, None


def _fsync_directory(directory: Path) -> dict[str, Any]:
    """Best-effort directory-entry durability barrier.

    POSIX filesystems generally support fsync on directory descriptors.
    Python's portable Windows APIs do not expose an equivalent directory
    handle flush, so Windows is reported as an explicit unsupported boundary.
    """
    if os.name == "nt":
        return {"supported": False, "synced": False, "detail": "directory fsync is not exposed by the portable Windows API"}
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(directory, flags)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
        return {"supported": True, "synced": True, "detail": "parent directory fsynced"}
    except OSError as exc:
        return {"supported": True, "synced": False, "detail": f"parent directory fsync failed: {exc}"}


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_size, left.st_mtime_ns) == (
        right.st_dev, right.st_ino, right.st_size, right.st_mtime_ns
    )


def _publish_restore_stage(stage: Path, dest: Path, digest: str) -> dict[str, Any]:
    """Verify an opened regular stage, fsync it, then publish without clobbering.

    File-descriptor identity checks narrow path replacement races. The final
    path-based hard-link operation still has a small cross-platform TOCTOU
    window against hostile same-user processes; this is not claimed as a
    security boundary against such an attacker.
    """
    if dest.is_symlink():
        return {"status": "conflict", "detail": "target is a symlink; stage and rescue retained"}
    if dest.exists():
        try:
            if hashlib.sha256(dest.read_bytes()).hexdigest() == digest:
                return {"status": "already-published", "detail": "target already contains the verified Rescue bytes; staging and rescue retained"}
        except OSError as exc:
            return {"status": "conflict", "detail": f"target exists but cannot be verified ({exc}); stage and rescue retained"}
        return {"status": "conflict", "detail": "target exists with different content; staging and rescue retained"}
    flags_read = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags_sync = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    read_fd = sync_fd = None
    try:
        read_fd = os.open(stage, flags_read)
        sync_fd = os.open(stage, flags_sync)
        read_st = os.fstat(read_fd)
        sync_st = os.fstat(sync_fd)
        path_st = os.stat(stage, follow_symlinks=False)
        if not stat.S_ISREG(read_st.st_mode) or not _same_file_identity(read_st, sync_st) or not _same_file_identity(read_st, path_st):
            return {"status": "unrecognized", "detail": "staging path is not a stable regular file; retained"}
        if read_st.st_nlink != 1:
            return {"status": "uncertain", "detail": "staging file has unexpected hard links; retained for inspection"}
        chunks = []
        while True:
            chunk = os.read(read_fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if hashlib.sha256(b"".join(chunks)).hexdigest() != digest:
            return {"status": "incomplete", "detail": "staging file does not match the rescue SHA-256; retained for inspection"}
        if not _same_file_identity(read_st, os.fstat(sync_fd)) or not _same_file_identity(read_st, os.stat(stage, follow_symlinks=False)):
            return {"status": "uncertain", "detail": "staging path changed during verification; retained"}
        os.fsync(sync_fd)
        if not _same_file_identity(read_st, os.stat(stage, follow_symlinks=False)):
            return {"status": "uncertain", "detail": "staging path changed before publication; retained"}
        try:
            os.link(stage, dest)
        except FileExistsError:
            try:
                existing = dest.read_bytes()
            except OSError as exc:
                return {"status": "conflict", "detail": f"target exists but cannot be verified ({exc}); stage and rescue retained"}
            if hashlib.sha256(existing).hexdigest() == digest and not dest.is_symlink():
                return {"status": "already-published", "detail": "target already contains the verified bytes; staging and rescue retained"}
            return {"status": "conflict", "detail": "target exists with different content; staging and rescue retained"}
        except OSError as exc:
            return {"status": "staged", "detail": f"verified staging file is ready but atomic no-clobber publish failed ({exc}); run rescue recover later"}
        try:
            published_st = os.stat(dest, follow_symlinks=False)
            if not _same_file_identity(read_st, published_st):
                return {"status": "uncertain", "detail": "published entry identity differs from the verified stage; preserve all evidence and inspect manually"}
        except OSError as exc:
            return {"status": "uncertain", "detail": f"published entry could not be identity-checked: {exc}"}
        directory_sync = _fsync_directory(dest.parent)
        if directory_sync["supported"] and not directory_sync["synced"]:
            return {"status": "published-not-durable", "detail": directory_sync["detail"] + "; target is present, stage and rescue retained"}
        if not directory_sync["supported"]:
            return {"status": "restored", "detail": "verified stage atomically published; Windows directory-entry durability is best-effort and not confirmed"}
        return {"status": "restored", "detail": "verified stage atomically published and parent directory fsynced"}
    except OSError as exc:
        return {"status": "uncertain", "detail": f"staging file could not be safely verified/fsynced: {exc}"}
    finally:
        if read_fd is not None:
            os.close(read_fd)
        if sync_fd is not None:
            os.close(sync_fd)


def _restore_rescue_impl(workspace: Path, rescue_rel: str) -> dict[str, Any]:
    """Stage, fsync, verify, then atomically publish without clobbering a target.

    A process crash can leave a deterministic sibling staging file. It is
    never mistaken for a restored target; rescue recover can verify and
    publish a complete stage, while incomplete stages remain untouched.
    """
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
        manifest_entry = _load_manifest(workspace).get(rescue_rel)
        orig = str(manifest_entry.get("orig", "")) if manifest_entry else ""
        dest, error = _safe_rescue_target(workspace, orig)
        if error or dest is None:
            return {"status": "rejected", "detail": error or "invalid target"}
        source = workspace / PurePosixPath(rescue_rel)
        try:
            data = source.read_bytes()
        except OSError as exc:
            return {"status": "uncertain", "detail": f"rescue source could not be read: {exc}"}
        digest = hashlib.sha256(data).hexdigest()
        if digest != item["sha256"]:
            return {"status": "rejected", "detail": "rescue content changed during restore"}
        stage = _restore_stage_path(dest, digest)
        if dest.is_symlink() or dest.exists():
            try:
                existing = dest.read_bytes()
            except OSError:
                existing = b""
            if hashlib.sha256(existing).hexdigest() == digest:
                return {"status": "already-published", "target": orig, "rescue": rescue_rel, "sha256": digest,
                        "detail": "target already contains the verified bytes; rescue retained"}
            return {"status": "conflict", "detail": f"target already exists; rescue preserved at {rescue_rel}"}
        if stage.is_symlink():
            return {"status": "rejected", "detail": "staging path is a symlink; not followed"}
        if stage.exists():
            published = _publish_restore_stage(stage, dest, digest)
            published.update({"target": orig, "rescue": rescue_rel, "sha256": digest})
            return published
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(stage, flags, 0o600)
        except FileExistsError:
            return {"status": "staged", "detail": "another recovery attempt created the staging path; run rescue recover"}
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("write made no forward progress")
                view = view[count:]
            os.fsync(fd)
        except OSError as exc:
            return {"status": "incomplete", "target": orig, "rescue": rescue_rel, "stage": _relative_posix(stage, workspace),
                    "sha256": digest, "detail": f"staging write/fsync failed ({exc}); stage and rescue retained for inspection"}
        finally:
            os.close(fd)
        # Data must be fsynced before syncing the directory entry; if this
        # barrier fails, retain a complete stage that a later recover can retry.
        stage_dir_sync = _fsync_directory(stage.parent)
        if stage_dir_sync["supported"] and not stage_dir_sync["synced"]:
            return {"status": "incomplete", "target": orig, "rescue": rescue_rel,
                    "stage": _relative_posix(stage, workspace), "sha256": digest,
                    "detail": stage_dir_sync["detail"] + "; complete stage and rescue retained"}
        published = _publish_restore_stage(stage, dest, digest)
        published.update({"target": orig, "rescue": rescue_rel, "stage": _relative_posix(stage, workspace), "sha256": digest})
        return published
    finally:
        lock.release()


def _recover_rescue_impl(workspace: Path) -> dict[str, Any]:
    """Reconcile deterministic restore stages against verified rescue sources."""
    workspace = Path(workspace).resolve()
    lock = SessionLock(workspace / ".jinlin" / "write.lock")
    try:
        lock.acquire()
    except SessionLockedError:
        return {"status": "locked", "items": []}
    try:
        listed = list_rescue_unlocked(workspace)
        items: list[dict[str, Any]] = []
        manifest = _load_manifest(workspace)
        for rescue in listed["rescued"]:
            entry = manifest.get(rescue["path"], {})
            dest, error = _safe_rescue_target(workspace, str(entry.get("orig", "")))
            if error or dest is None:
                continue
            digest = rescue["sha256"]
            stage = _restore_stage_path(dest, digest)
            if not stage.exists() and not stage.is_symlink():
                continue
            if stage.is_symlink():
                items.append({"rescue": rescue["path"], "stage": _relative_posix(stage, workspace),
                              "status": "unrecognized", "detail": "staging path is a symlink; not followed"})
                continue
            result = _publish_restore_stage(stage, dest, digest)
            result.update({"rescue": rescue["path"], "target": str(entry.get("orig", "")),
                           "stage": _relative_posix(stage, workspace), "sha256": digest})
            items.append(result)
        # A stage without a currently verified Rescue+manifest record is
        # evidence only. Never infer its target or publish/delete it.
        expected: set[str] = set()
        for rescue in listed["rescued"]:
            entry = manifest.get(rescue["path"], {})
            target, target_error = _safe_rescue_target(workspace, str(entry.get("orig", "")))
            if target_error or target is None:
                continue
            expected.add(_relative_posix(_restore_stage_path(target, rescue["sha256"]), workspace))
        orphaned: list[dict[str, Any]] = []
        for root, dirs, files in os.walk(workspace, followlinks=False):
            # Include matching directories as suspicious artifacts too;
            # os.walk does not follow symlink directories with followlinks=False.
            candidates = list(files) + list(dirs)
            for name in candidates:
                if not RESTORE_STAGE_RE.match(name):
                    continue
                candidate = Path(root) / name
                rel = _relative_posix(candidate, workspace)
                if rel not in expected:
                    orphaned.append({
                        "path": rel,
                        "kind": "symlink" if candidate.is_symlink() else ("directory" if candidate.is_dir() else "file"),
                        "status": "orphaned",
                        "detail": "no currently verified Rescue+manifest record authorizes this stage; report-only",
                    })
        orphaned.sort(key=lambda item: item["path"])
        return {"status": "ok", "items": items, "orphaned": orphaned}
    finally:
        lock.release()


def _quarantine_and_unlink_verified(
    path: Path, digest: str, expected_identity: tuple[int, int, int, int] | None = None
) -> tuple[bool, str]:
    """Move a candidate out of its public name, verify the moved object, then unlink.

    If the moved object is not the expected regular file, retain it in
    quarantine and attempt a no-clobber link back. This avoids deleting a
    replacement merely because it occupied a previously scanned pathname.
    """
    if path.is_symlink():
        return False, "candidate is a symlink"
    try:
        before = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        return False, f"candidate identity could not be captured: {exc}"
    before_identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    if expected_identity is not None and before_identity != expected_identity:
        return False, "candidate identity changed after eligibility scan"
    quarantine = path.with_name(f".{path.name}.jinlin-clean-{uuid.uuid4().hex[:8]}")
    try:
        os.replace(path, quarantine)
    except OSError as exc:
        return False, f"could not move candidate to quarantine: {exc}"
    try:
        st = os.stat(quarantine, follow_symlinks=False)
        moved_identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        if not stat.S_ISREG(st.st_mode) or quarantine.is_symlink():
            return False, f"quarantined object is not a regular file; retained at {quarantine.name}"
        if moved_identity != before_identity:
            link_no_clobber(quarantine, path)
            return False, f"quarantined object identity differs from scanned candidate; evidence retained at {quarantine.name}"
        data = quarantine.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            link_no_clobber(quarantine, path)
            return False, f"quarantined object failed SHA-256 verification; evidence retained at {quarantine.name}"
        os.unlink(quarantine)
        _fsync_directory(path.parent)
        return True, "verified object removed"
    except OSError as exc:
        return False, f"quarantined object could not be safely verified/removed ({exc}); retained at {quarantine.name}"


def _clean_rescue_impl(workspace: Path, apply: bool = False) -> dict[str, Any]:
    """Dry-run or explicitly clean redundant restore evidence.

    A Rescue and its stage are eligible only when the restored target is a
    regular, non-symlink file whose bytes match the manifest-bound Rescue
    SHA-256. Deletion uses quarantine-and-verify, never a bare name-based
    unlink. Default behavior is report-only.
    """
    workspace = Path(workspace).resolve()
    report: dict[str, Any] = {
        "status": "dry-run", "eligible": [], "removed": [], "failed": [],
        "manifest_degraded": [], "unrecognized": [], "quarantined": [],
    }
    lock = SessionLock(workspace / ".jinlin" / "write.lock")
    try:
        lock.acquire()
    except SessionLockedError:
        report["status"] = "locked"
        return report
    try:
        listed = list_rescue_unlocked(workspace)
        report["unrecognized"] = [item["path"] for item in listed["unrecognized"]]
        # A process may stop after quarantine rename but before unlink. Such
        # objects are evidence-only until a human inspects them.
        for root, dirs, files in os.walk(workspace, followlinks=False):
            dirs[:] = [name for name in dirs if name not in _SKIP_DIRS]
            for name in list(files) + list(dirs):
                if CLEAN_QUARANTINE_RE.match(name):
                    candidate = Path(root) / name
                    report["quarantined"].append({
                        "path": _relative_posix(candidate, workspace),
                        "kind": "symlink" if candidate.is_symlink() else ("directory" if candidate.is_dir() else "file"),
                    })
        manifest = _load_manifest(workspace)
        report["quarantined"] = _inspect_clean_quarantines(workspace, manifest)
        eligible: list[dict[str, Any]] = []
        for rescue in listed["rescued"]:
            entry = manifest.get(rescue["path"], {})
            target, error = _safe_rescue_target(workspace, str(entry.get("orig", "")))
            if error or target is None or target.is_symlink():
                report["failed"].append({"rescue": rescue["path"], "reason": error or "target is a symlink"})
                continue
            try:
                target_st = os.stat(target, follow_symlinks=False)
                if not stat.S_ISREG(target_st.st_mode):
                    raise OSError("target is not a regular file")
                if hashlib.sha256(target.read_bytes()).hexdigest() != rescue["sha256"]:
                    raise OSError("target content does not match Rescue SHA-256")
            except OSError as exc:
                report["failed"].append({"rescue": rescue["path"], "reason": f"restored target is not verified: {exc}"})
                continue
            stage = _restore_stage_path(target, rescue["sha256"])
            stage_exists = stage.exists() or stage.is_symlink()
            if stage_exists:
                if stage.is_symlink():
                    report["failed"].append({"rescue": rescue["path"], "reason": "staging path is a symlink"})
                    continue
                try:
                    stage_st = os.stat(stage, follow_symlinks=False)
                    if not stat.S_ISREG(stage_st.st_mode) or hashlib.sha256(stage.read_bytes()).hexdigest() != rescue["sha256"]:
                        raise OSError("staging file does not match Rescue SHA-256")
                except OSError as exc:
                    report["failed"].append({"rescue": rescue["path"], "reason": f"staging file is not verified: {exc}"})
                    continue
            eligible.append({"rescue": rescue["path"], "target": rescue["orig"],
                             "stage": _relative_posix(stage, workspace) if stage_exists else None,
                             "stage_identity": ((stage_st.st_dev, stage_st.st_ino, stage_st.st_size, stage_st.st_mtime_ns)
                                               if stage_exists else None),
                             "rescue_identity": ((ident.get("dev"), ident.get("ino"), ident.get("size"), ident.get("mtime_ns"))
                                                 if isinstance((ident := entry.get("identity")), dict) else None),
                             "sha256": rescue["sha256"],
                             "transaction_id": entry.get("transaction_id")})
        report["eligible"] = eligible
        if not apply:
            return report
        report["status"] = "applied"
        for item in eligible:
            target, error = _safe_rescue_target(workspace, item["target"])
            if error or target is None:
                report["failed"].append({"rescue": item["rescue"], "reason": error or "invalid target"})
                continue
            # Recheck the target immediately before removing any evidence.
            try:
                if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != item["sha256"]:
                    raise OSError("target changed after eligibility scan")
            except OSError as exc:
                report["failed"].append({"rescue": item["rescue"], "reason": f"target changed; evidence retained: {exc}"})
                continue
            if item["stage"]:
                stage = workspace / PurePosixPath(item["stage"])
                ok, detail = _quarantine_and_unlink_verified(stage, item["sha256"], item.get("stage_identity"))
                if not ok:
                    report["failed"].append({"rescue": item["rescue"], "reason": detail})
                    continue
            rescue_path = workspace / PurePosixPath(item["rescue"])
            ok, detail = _quarantine_and_unlink_verified(rescue_path, item["sha256"], item.get("rescue_identity"))
            if not ok:
                report["failed"].append({"rescue": item["rescue"], "reason": detail})
                continue
            if not forget_trash(workspace, rescue_path):
                report["manifest_degraded"].append(item["rescue"])
            report["removed"].append(item["rescue"])
        return report
    finally:
        lock.release()



def _quarantine_restore_stage_path(candidate: Path, digest: str) -> Path:
    """Deterministic private staging name for an interrupted quarantine recovery."""
    return candidate.with_name(f".{candidate.name}.jinlin-quarantine-restore-{digest[:16]}")


def _read_verified_regular(path: Path, digest: str, expected_identity: tuple[int, int, int, int] | None = None) -> tuple[bytes | None, tuple[int, int, int, int] | None, str | None]:
    """Read one stable, non-symlink regular file and verify its bytes and identity."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open(path, flags)
        before = os.fstat(fd)
        identity = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        if not stat.S_ISREG(before.st_mode):
            return None, None, "source is not a regular file"
        if expected_identity is not None and identity != expected_identity:
            return None, identity, "source identity does not match the manifest"
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        path_after = os.stat(path, follow_symlinks=False)
        if not _same_file_identity(before, after) or not _same_file_identity(before, path_after):
            return None, identity, "source changed while being verified"
        data = b"".join(chunks)
        if hashlib.sha256(data).hexdigest() != digest:
            return None, identity, "source SHA-256 does not match the manifest"
        return data, identity, None
    except OSError as exc:
        return None, None, f"source could not be safely opened/read: {exc}"
    finally:
        if fd is not None:
            os.close(fd)


def _read_stable_regular(path: Path) -> tuple[bytes | None, tuple[int, int, int, int] | None, str | None]:
    """Read a stable regular file without requiring its final digest to match."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = None
    try:
        fd = os.open(path, flags)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None, None, "stage is not a regular file"
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(fd)
        path_after = os.stat(path, follow_symlinks=False)
        if not _same_file_identity(before, after) or not _same_file_identity(before, path_after):
            return None, None, "stage changed while being read"
        return b"".join(chunks), (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns), None
    except OSError as exc:
        return None, None, f"stage could not be safely read: {exc}"
    finally:
        if fd is not None:
            os.close(fd)


def _quarantine_recover_one_locked(workspace: Path, item: dict[str, Any], manifest: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Restore one verified quarantine as an independent copy, without clobbering."""
    quarantine_rel = str(item.get("path", ""))
    candidate_rel = str(item.get("candidate", ""))
    digest = str(item.get("sha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return {"path": quarantine_rel, "candidate": candidate_rel, "status": "rejected", "detail": "missing or invalid SHA-256"}
    rel = PurePosixPath(quarantine_rel)
    candidate_pure = PurePosixPath(candidate_rel)
    if (rel.is_absolute() or candidate_pure.is_absolute()
            or any(part in ("", ".", "..") for part in rel.parts + candidate_pure.parts)
            or chr(92) in quarantine_rel or chr(92) in candidate_rel):
        return {"path": quarantine_rel, "candidate": candidate_rel, "status": "rejected", "detail": "workspace-relative path is invalid"}
    source = workspace.joinpath(*rel.parts)
    candidate = workspace.joinpath(*candidate_pure.parts)
    try:
        candidate.parent.resolve(strict=True).relative_to(workspace)
    except (OSError, ValueError):
        return {"path": quarantine_rel, "candidate": candidate_rel, "status": "rejected", "detail": "candidate parent is missing or outside the workspace"}
    candidate_kind = item.get("candidate_kind")
    entry = manifest.get(candidate_rel) if candidate_kind == "rescue" else None
    original_identity = None
    if candidate_kind == "rescue":
        if not isinstance(entry, dict) or entry.get("kind") != "rescue":
            return {"path": quarantine_rel, "candidate": candidate_rel, "status": "rejected", "detail": "Rescue manifest entry is missing"}
        ident = entry.get("quarantine_identity", entry.get("identity"))
        if isinstance(ident, dict) and all(isinstance(ident.get(k), int) for k in ("dev", "ino", "size", "mtime_ns")):
            original_identity = (ident["dev"], ident["ino"], ident["size"], ident["mtime_ns"])
    data, source_identity, error = _read_verified_regular(source, digest, original_identity)
    if error or data is None or source_identity is None:
        return {"path": quarantine_rel, "candidate": candidate_rel, "status": "rejected", "detail": error or "quarantine verification failed"}
    stage = _quarantine_restore_stage_path(candidate, digest)

    def reconcile_published(newly_published: bool = False) -> dict[str, Any]:
        if candidate.is_symlink():
            return {"path": quarantine_rel, "candidate": candidate_rel, "status": "conflict", "detail": "candidate is a symlink"}
        published, published_identity, published_error = _read_verified_regular(candidate, digest)
        if published_error or published is None or published_identity is None:
            return {"path": quarantine_rel, "candidate": candidate_rel, "status": "conflict", "detail": published_error or "candidate does not match"}
        if published_identity == source_identity:
            return {"path": quarantine_rel, "candidate": candidate_rel, "status": "conflict",
                    "detail": "candidate shares the quarantine inode; independent evidence cannot be guaranteed"}
        if candidate_kind == "rescue":
            recorded_candidate = entry.get("identity") if isinstance(entry, dict) else None
            recorded_matches = (
                isinstance(recorded_candidate, dict)
                and published_identity == (
                    recorded_candidate.get("dev"), recorded_candidate.get("ino"),
                    recorded_candidate.get("size"), recorded_candidate.get("mtime_ns")
                )
            )
            stage_matches = False
            if stage.exists() and not stage.is_symlink():
                try:
                    stage_st = os.stat(stage, follow_symlinks=False)
                    candidate_st = os.stat(candidate, follow_symlinks=False)
                    stage_matches = _same_file_identity(stage_st, candidate_st)
                except OSError:
                    stage_matches = False
            if not recorded_matches and not stage_matches:
                return {"path": quarantine_rel, "candidate": candidate_rel, "status": "conflict",
                        "detail": "occupied candidate has no matching manifest or recovery-stage identity"}
            updated = dict(entry)
            updated["quarantine_identity"] = {
                "dev": source_identity[0], "ino": source_identity[1], "size": source_identity[2],
                "mtime_ns": source_identity[3], "sha256": digest,
            }
            updated["identity"] = {
                "dev": published_identity[0], "ino": published_identity[1], "size": published_identity[2],
                "mtime_ns": published_identity[3], "sha256": digest,
            }
            new_manifest = dict(manifest)
            new_manifest[candidate_rel] = updated
            if not _write_manifest_atomic(workspace, new_manifest):
                return {"path": quarantine_rel, "candidate": candidate_rel, "status": "uncertain",
                        "detail": "candidate is published but manifest identity update failed; quarantine retained for retry"}
            manifest.clear()
            manifest.update(new_manifest)
        # A leftover stage is removed only when it is the same inode as the
        # published candidate. A different object is retained for inspection.
        try:
            if stage.exists() or stage.is_symlink():
                stage_st = os.stat(stage, follow_symlinks=False)
                candidate_st = os.stat(candidate, follow_symlinks=False)
                if _same_file_identity(stage_st, candidate_st):
                    os.unlink(stage)
                    _fsync_directory(candidate.parent)
        except OSError:
            pass
        return {"path": quarantine_rel, "candidate": candidate_rel, "candidate_kind": candidate_kind,
                "status": "restored" if newly_published else "already-restored", "sha256": digest,
                "detail": "verified candidate published without clobbering; quarantine evidence retained" if newly_published
                else "verified candidate already exists; quarantine evidence retained"}

    if candidate.exists() or candidate.is_symlink():
        return reconcile_published()

    if stage.is_symlink():
        return {"path": quarantine_rel, "candidate": candidate_rel, "status": "rejected", "detail": "recovery stage is a symlink"}
    if stage.exists():
        staged, stage_identity, stage_error = _read_verified_regular(stage, digest)
        if stage_error or staged is None or stage_identity is None:
            partial, partial_identity, partial_error = _read_stable_regular(stage)
            if partial_error or partial is None or partial_identity is None or len(partial) > len(data) or data[:len(partial)] != partial:
                return {"path": quarantine_rel, "candidate": candidate_rel, "status": "rejected", "stage": _relative_posix(stage, workspace),
                        "detail": f"existing recovery stage is not a verified prefix of the quarantine: {partial_error or stage_error}"}
            flags_append = os.O_WRONLY | os.O_APPEND | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                append_fd = os.open(stage, flags_append)
                try:
                    opened = os.fstat(append_fd)
                    opened_identity = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                    if opened_identity != partial_identity:
                        raise OSError("recovery stage identity changed before continuation")
                    view = memoryview(data[len(partial):])
                    while view:
                        count = os.write(append_fd, view)
                        if count <= 0:
                            raise OSError("stage continuation made no forward progress")
                        view = view[count:]
                    os.fsync(append_fd)
                finally:
                    os.close(append_fd)
            except OSError as exc:
                return {"path": quarantine_rel, "candidate": candidate_rel, "status": "incomplete",
                        "stage": _relative_posix(stage, workspace), "detail": f"partial stage could not be safely continued: {exc}"}
            staged, stage_identity, stage_error = _read_verified_regular(stage, digest)
            if stage_error or staged is None or stage_identity is None:
                return {"path": quarantine_rel, "candidate": candidate_rel, "status": "incomplete",
                        "stage": _relative_posix(stage, workspace), "detail": stage_error or "continued stage verification failed"}
    else:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        try:
            fd = os.open(stage, flags, 0o600)
        except FileExistsError:
            return {"path": quarantine_rel, "candidate": candidate_rel, "status": "uncertain",
                    "detail": "another recovery attempt created the deterministic stage; retry inspection"}
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                if count <= 0:
                    raise OSError("stage write made no forward progress")
                view = view[count:]
            os.fsync(fd)
        except OSError as exc:
            return {"path": quarantine_rel, "candidate": candidate_rel, "status": "incomplete",
                    "stage": _relative_posix(stage, workspace), "detail": f"stage write/fsync failed: {exc}; stage and quarantine retained"}
        finally:
            os.close(fd)
        staged, stage_identity, stage_error = _read_verified_regular(stage, digest)
        if stage_error or staged is None or stage_identity is None:
            return {"path": quarantine_rel, "candidate": candidate_rel, "status": "incomplete",
                    "stage": _relative_posix(stage, workspace), "detail": stage_error or "stage verification failed"}
        sync = _fsync_directory(stage.parent)
        if sync["supported"] and not sync["synced"]:
            return {"path": quarantine_rel, "candidate": candidate_rel, "status": "incomplete",
                    "stage": _relative_posix(stage, workspace), "detail": sync["detail"] + "; stage and quarantine retained"}

    try:
        os.link(stage, candidate)
    except FileExistsError:
        return reconcile_published()
    except OSError as exc:
        return {"path": quarantine_rel, "candidate": candidate_rel, "status": "staged",
                "stage": _relative_posix(stage, workspace), "detail": f"no-clobber publication failed: {exc}; retry is safe"}
    result = reconcile_published(newly_published=True)
    if result["status"] in ("restored", "already-restored", "uncertain"):
        result["stage"] = _relative_posix(stage, workspace)
    return result


def recover_clean_quarantines(workspace: Path, apply: bool = False) -> dict[str, Any]:
    """Preview or safely recover manifest-backed Rescue cleanup quarantines.

    Dry-run performs no writes. Apply reclassifies all evidence while holding
    the workspace lock, rejects ambiguous/modified/legacy evidence, copies
    through a deterministic fsynced stage, and publishes with atomic no-clobber
    hard-link semantics. The original quarantine remains as independent evidence.
    """
    workspace = Path(workspace).resolve()
    report: dict[str, Any] = {"status": "dry-run", "items": [], "recovered": [], "blocked": []}
    if not apply:
        inspected = inspect_rescue(workspace)
        if inspected.get("status") == "locked":
            return {"status": "locked", "items": [], "recovered": [], "blocked": []}
        report["items"] = inspected.get("quarantined", [])
        report["blocked"] = [x for x in report["items"] if x.get("classification") not in ("safe-to-restore", "already-restored")]
        return report
    lock = SessionLock(workspace / ".jinlin" / "write.lock")
    try:
        lock.acquire()
    except SessionLockedError:
        report["status"] = "locked"
        return report
    try:
        manifest = _load_manifest(workspace)
        items = _inspect_clean_quarantines(workspace, manifest)
        report["status"] = "applied"
        report["items"] = items
        claimed: set[str] = set()
        for item in items:
            classification = item.get("classification")
            candidate = str(item.get("candidate", ""))
            if candidate in claimed:
                result = {"path": item["path"], "candidate": candidate, "status": "ambiguous",
                          "detail": "another quarantine item already claims this candidate"}
            else:
                claimed.add(candidate)
                if classification not in ("safe-to-restore", "already-restored"):
                    result = {"path": item["path"], "candidate": candidate, "status": "blocked",
                              "classification": classification, "detail": item.get("reason", "not eligible for automatic recovery")}
                else:
                    result = _quarantine_recover_one_locked(workspace, item, manifest)
            (report["recovered"] if result["status"] in ("restored", "already-restored") else report["blocked"]).append(result)
            _audit_result(workspace, "quarantine-recover", result, rescue=item.get("rescue"),
                          target=item.get("target"), quarantine=item.get("path"), candidate=candidate,
                          sha256=item.get("sha256"), txid=item.get("transaction_id"))
        if report["blocked"]:
            report["status"] = "attention-required"
        return report
    finally:
        lock.release()


def restore_rescue(workspace: Path, rescue_rel: str) -> dict[str, Any]:
    result = _restore_rescue_impl(workspace, rescue_rel)
    if result.get("status") == "locked":
        return result
    return _audit_result(workspace, "restore", result, rescue=rescue_rel)


def recover_rescue(workspace: Path) -> dict[str, Any]:
    result = _recover_rescue_impl(workspace)
    if result.get("status") == "locked":
        return result
    for item in result.get("items", []):
        _audit_result(workspace, "recover", item, rescue=item.get("rescue"), target=item.get("target"))
    for item in result.get("orphaned", []):
        _audit_result(workspace, "recover", item, target=item.get("path"))
    return result


def clean_rescue(workspace: Path, apply: bool = False) -> dict[str, Any]:
    result = _clean_rescue_impl(workspace, apply=apply)
    if result.get("status") == "locked":
        return result
    action = "clean" if apply else "clean-preview"
    for item in result.get("eligible", []):
        if not apply:
            _audit_result(workspace, action, {"status": "dry-run"}, rescue=item.get("rescue"),
                          target=item.get("target"), stage=item.get("stage"), sha256=item.get("sha256"))
    eligible_by_rescue = {item.get("rescue"): item for item in result.get("eligible", [])}
    for path in result.get("removed", []):
        item = eligible_by_rescue.get(path, {})
        _audit_result(workspace, "clean", {"status": "removed", "txid": item.get("transaction_id")}, rescue=path,
                      target=item.get("target"), stage=item.get("stage"), sha256=item.get("sha256"),
                      txid=item.get("transaction_id"))
    for item in result.get("failed", []):
        eligible = eligible_by_rescue.get(item.get("rescue"), {})
        _audit_result(workspace, "clean", {"status": "cleanup-blocked", "detail": item.get("reason"),
                          "txid": eligible.get("transaction_id")},
                      rescue=item.get("rescue"), target=eligible.get("target"),
                      stage=eligible.get("stage"), sha256=eligible.get("sha256"),
                      txid=eligible.get("transaction_id"))
    return result


def inspect_rescue(workspace: Path) -> dict[str, Any]:
    """Read-only consistency report across Rescue, stages, quarantine and audit."""
    workspace = Path(workspace).resolve()
    lock = SessionLock(workspace / ".jinlin" / "write.lock")
    try:
        lock.acquire()
    except SessionLockedError:
        return {"status": "locked"}
    try:
        listed = list_rescue_unlocked(workspace)
        manifest = _load_manifest(workspace)
        expected: dict[str, dict[str, Any]] = {}
        for rescue in listed["rescued"]:
            entry = manifest.get(rescue["path"], {})
            target, error = _safe_rescue_target(workspace, str(entry.get("orig", "")))
            if error or target is None:
                continue
            stage = _restore_stage_path(target, rescue["sha256"])
            expected[_relative_posix(stage, workspace)] = {
                "rescue": rescue["path"], "target": str(entry.get("orig", "")), "sha256": rescue["sha256"]
            }
        stages: list[dict[str, Any]] = []
        orphaned: list[dict[str, Any]] = []
        quarantined: list[dict[str, Any]] = []
        for root, dirs, files in os.walk(workspace, followlinks=False):
            dirs[:] = [name for name in dirs if name not in _SKIP_DIRS]
            for name in list(files) + list(dirs):
                path = Path(root) / name
                rel = _relative_posix(path, workspace)
                if RESTORE_STAGE_RE.match(name):
                    item = {"path": rel, "kind": "symlink" if path.is_symlink() else ("directory" if path.is_dir() else "file")}
                    if rel in expected:
                        item.update(expected[rel])
                        item["state"] = "STAGED"
                        try:
                            if not path.is_symlink() and path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]:
                                item["integrity"] = "sha256-valid"
                            else:
                                item["integrity"] = "invalid-or-unreadable"
                        except OSError:
                            item["integrity"] = "unreadable"
                        stages.append(item)
                    else:
                        item["state"] = "ORPHANED"
                        orphaned.append(item)
                if CLEAN_QUARANTINE_RE.match(name):
                    quarantined.append({"path": rel, "kind": "symlink" if path.is_symlink() else ("directory" if path.is_dir() else "file"),
                                        "state": "CLEANUP_INTERRUPTED"})
        quarantined = _inspect_clean_quarantines(workspace, manifest)
        audit = _read_audit_consistent(workspace)
        states = _transaction_states(audit["events"])
        issues = list(audit["issues"])
        incomplete_stages = [
            stage for stage in stages
            if stage.get("integrity") != "sha256-valid"
        ]
        if incomplete_stages:
            issues.append({
                "kind": "incomplete-or-unreadable-stage",
                "count": len(incomplete_stages),
                "paths": [stage["path"] for stage in incomplete_stages],
            })
        for transaction in states:
            if transaction.get("transition_issues"):
                issues.append({"kind": "invalid-state-transition", "txid": transaction["txid"],
                               "details": transaction["transition_issues"]})
        if listed["unrecognized"]:
            issues.append({"kind": "unrecognized-rescue", "count": len(listed["unrecognized"])})
        if orphaned:
            issues.append({"kind": "orphaned-stage", "count": len(orphaned)})
        if quarantined:
            issues.append({"kind": "interrupted-cleanup", "count": len(quarantined)})
        return {
            "status": "ok" if not issues else "attention-required",
            "counts": {"verified_rescue": len(listed["rescued"]), "unrecognized_rescue": len(listed["unrecognized"]),
                       "stages": len(stages), "incomplete_stages": len(incomplete_stages),
                       "orphaned_stages": len(orphaned), "quarantined": len(quarantined),
                       "transactions": len(states),
                       "invalid_transactions": sum(bool(x.get("transition_issues") or x.get("identity_issues")) for x in states),
                       "audit_last_sequence": max((int(x.get("seq", 0)) for x in audit["events"] if type(x.get("seq")) is int), default=0),
                        "quarantine_safe_to_restore": sum(x.get("classification") == "safe-to-restore" for x in quarantined),
                        "quarantine_already_restored": sum(x.get("classification") == "already-restored" for x in quarantined),
                        "quarantine_conflicts": sum(x.get("classification") == "target-conflict" for x in quarantined),
                        "quarantine_unverified": sum(x.get("classification") in ("identity-mismatch", "ambiguous", "unrecognized", "legacy-unverified") for x in quarantined),
                       "audit_events": len(audit["events"]), "audit_issues": len(audit["issues"])},
            "rescued": listed["rescued"], "unrecognized": listed["unrecognized"],
            "stages": stages, "orphaned": orphaned, "quarantined": quarantined,
            "transactions": states, "audit": {"path": AUDIT_REL.as_posix(), "last_hash": audit["last_hash"],
                                               "issues": audit["issues"]}, "issues": issues,
        }
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
