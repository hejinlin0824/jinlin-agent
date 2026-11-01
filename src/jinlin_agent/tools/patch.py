"""Editing: `apply_patch` (Codex V4A envelope, multi-file, atomic) and
`edit_file` (exact old/new string, Anthropic style).

Both share the same safety engine:
  * read-before-write: the session must have read the current content
  * hash check: content on disk must still match what was read
  * exact matching only (no fuzzy matching — silent corruption is worse than a
    failed edit)
  * first match must be unique (edit_file) / hunk must match exactly once
  * BOM and line endings preserved; updates write atomically (temp file +
    os.replace with a pre-replace hash re-check), new files are established
    with an exclusive create (O_CREAT | O_EXCL) so they can never overwrite
    a concurrent creator
  * the write phase is serialized across jinlin sessions by the workspace
    write lock (<workspace>/.jinlin/write.lock); the residual TOCTOU window
    against uncoordinated external processes is part of the documented
    security model
  * a multi-file patch validates everything before writing anything
"""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from ..errors import (
    PATCH_CONFLICT,
    PATCH_PARSE_ERROR,
    FILE_NOT_READ,
    FILE_CHANGED_EXTERNALLY,
    RECOVERY_UNCERTAIN,
    WORKSPACE_LOCKED,
    AgentError,
)
from ..locks import SessionLock, SessionLockedError
from ..trash import forget_trash, record_trash
from .base import ToolContext, ToolResult, ToolSpec
from .registry import ToolRegistry


# --------------------------------------------------------------------------- text io


@contextmanager
def _workspace_write_guard(ctx: ToolContext) -> Iterator[None]:
    """Serialize the write phase across jinlin sessions on one workspace.

    The lock is what turns the pre-write hash check into a hard guarantee
    between sessions: while it is held, no other jinlin process can enter its
    own write phase, so no hash-visible change can slip in. It says nothing
    about uncoordinated external processes — that residual window is part of
    the documented security model.
    """
    lock = SessionLock(Path(ctx.workspace) / ".jinlin" / "write.lock")
    try:
        lock.acquire()
    except SessionLockedError as exc:
        raise AgentError(
            WORKSPACE_LOCKED,
            "another jinlin session holds this workspace's write lock; "
            "retry once it finishes (edits are serialized per workspace)",
            source="edit",
            retriable=True,
        ) from exc
    try:
        yield
    finally:
        lock.release()


@dataclass
class FileText:
    path: Path
    rel: str
    data: bytes
    text: str
    eol: str
    had_bom: bool
    trailing_newline: bool
    lines: list[str]

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def load_text(path: Path, rel: str, max_bytes: int) -> FileText:
    data = path.read_bytes()
    if len(data) > max_bytes:
        raise AgentError("FILE_TOO_LARGE", f"{rel}: {len(data)} bytes exceeds limit", source="edit")
    had_bom = data.startswith(b"\xef\xbb\xbf")
    try:
        text = data.decode("utf-8-sig") if had_bom else data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentError("BINARY_FILE", f"{rel}: not valid UTF-8 ({exc})", source="edit") from exc
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    eol = "\r\n" if crlf > lf else "\n"
    normalized = text.replace("\r\n", "\n")
    trailing = normalized.endswith("\n")
    lines = normalized.split("\n")
    if trailing:
        lines = lines[:-1]
    return FileText(path, rel, data, normalized, eol, had_bom, trailing, lines)


def render(f: FileText, lines: list[str]) -> bytes:
    body = f.eol.join(lines)
    if f.trailing_newline:
        body += f.eol
    raw = body.encode("utf-8")
    if f.had_bom:
        raw = b"\xef\xbb\xbf" + raw
    return raw


def atomic_write(
    path: Path,
    data: bytes,
    mode_from: Path | None = None,
    expected_sha256: str | None = None,
    create: bool = False,
) -> None:
    """Write atomically; re-check the target hash immediately before replacing.

    ``create=True`` establishes the file with an exclusive create
    (``O_CREAT | O_EXCL``) and never touches ``os.replace``, so a file created
    concurrently can never be overwritten: the write fails with PATCH_CONFLICT
    instead. This is an atomic no-clobber guarantee provided by the filesystem.

    With ``expected_sha256`` the target is re-read immediately before the
    replace and the written file is verified afterwards. That check *narrows*
    the check-then-replace TOCTOU window; it cannot eliminate it against an
    uncoordinated external process (portable file APIs have no atomic
    compare-and-swap on regular files). Jinlin sessions are mutually excluded
    by the workspace write lock (``<workspace>/.jinlin/write.lock``); the
    residual window is documented in the security model
    (``docs/REPORT-AND-HANDBOOK.md`` §16).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if create:
        _atomic_create_new(path, data)
        return
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode_from is not None and path.exists():
            try:
                os.chmod(tmp_name, stat.S_IMODE(mode_from.stat().st_mode))
            except OSError:
                pass
        if expected_sha256 is not None:
            current = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            if current != expected_sha256:
                raise AgentError(
                    FILE_CHANGED_EXTERNALLY,
                    f"{path.name} changed just before the write (expected {expected_sha256[:12]}, found {str(current)[:12]})",
                    source="edit",
                )
        os.replace(tmp_name, path)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != hashlib.sha256(data).hexdigest():
            raise AgentError(TOOL_EXECUTION_FAILED, f"{path.name}: written content failed verification", source="edit")
        # A .pyc records its source mtime in whole seconds. An edit that lands in
        # the same second as the previous import (common on fast CI/mock runs) would
        # otherwise keep re-using stale bytecode, so drop bytecode for this file.
        _invalidate_bytecode(path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _atomic_create_new(path: Path, data: bytes) -> None:
    """Create ``path`` exclusively and write ``data`` (atomic no-clobber).

    ``O_CREAT | O_EXCL`` makes establishment and the does-it-exist decision a
    single filesystem operation on both Windows and POSIX. If the write fails,
    the partial file is removed **only while the path still provably holds the
    inode this call created** — identity is the ``(st_dev, st_ino, st_size,
    st_mtime_ns)`` tuple captured from the open fd after the bytes landed;
    plain ``(dev, ino)`` is not enough because Linux filesystems hand the
    just-freed inode straight back to the next create, which would make a
    concurrent replacement look like our file. A path that was replaced, or a
    failure before the identity could be captured, is left untouched and
    surfaces as RECOVERY_UNCERTAIN instead of deleting by name.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    except FileExistsError as exc:
        raise AgentError(
            PATCH_CONFLICT,
            f"{path.name} was created concurrently; refusing to overwrite",
            source="edit",
        ) from exc
    identity: tuple[int, int, int, int] | None = None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            st = os.fstat(fh.fileno())
            identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            os.fsync(fh.fileno())
    except BaseException as exc:
        provably_ours = False
        if identity is not None:
            try:
                current = os.stat(path)
                provably_ours = (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                ) == identity
            except OSError:
                provably_ours = False  # path vanished — nothing of ours to clean
        if provably_ours:
            try:
                os.unlink(path)
            except OSError:
                pass
            raise
        # the path no longer provably holds our file: never delete by name
        raise AgentError(
            RECOVERY_UNCERTAIN,
            f"{path.name}: the create failed and the path no longer provably holds "
            "the file this call created; it was left in place for inspection",
            source="edit",
        ) from exc
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != hashlib.sha256(data).hexdigest():
        raise AgentError(TOOL_EXECUTION_FAILED, f"{path.name}: written content failed verification", source="edit")
    _invalidate_bytecode(path)


def _invalidate_bytecode(path: Path) -> None:
    """Remove cached bytecode for an edited Python source file (best effort)."""
    if path.suffix.lower() != ".py":
        return
    cache_dir = path.parent / "__pycache__"
    if not cache_dir.is_dir():
        return
    try:
        for cached in cache_dir.glob(f"{path.stem}.*.pyc"):
            cached.unlink()
    except OSError:
        pass


def _restore_no_clobber(source: Path, dest: Path) -> bool:
    """Move ``source`` onto ``dest`` without ever replacing an entry — and
    without unlinking the source path afterwards.

    ``os.link`` atomically creates ``dest`` only when it is absent, so a file
    a concurrent writer placed at ``dest`` can never be replaced. The source
    name is deliberately **left in place** (plan A of the round-6 review):
    deleting it by name after the link could remove a file an external
    process created at that path between the two directory operations. The
    redundant entry is harmless, keeps the move-time content recoverable for
    inspection, and is reported by the caller as ``redundant_trash`` —
    safe-to-remove litter for a controlled cleanup, never a target for
    by-name deletion inside the recovery path.

    Returns False — keeping ``source`` intact — when ``dest`` is occupied or
    the filesystem cannot hard-link; callers must then report
    RECOVERY_UNCERTAIN. An overwriting rename is deliberately *not* used as
    a fallback.
    """
    try:
        os.link(source, dest)
    except OSError:
        return False  # dest claimed concurrently, or fs without hardlink support
    return True


def _rollback_transaction(
    applied: list[dict[str, Any]], workspace: Path, manifest_degraded: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Revert only mutations this transaction actually made *and* still owns.

    Ownership is verified by content hash, and the live path is never deleted
    or overwritten in place: the current file is first renamed to a
    transaction-scoped trash name (atomic, content-preserving), the trash is
    hashed, and only when it matches what our write produced is it removed or
    replaced. Every put-back — trash or backup onto the original path — goes
    through `_restore_no_clobber`: if a concurrent writer recreated the path
    while our content sat aside, both sides are kept and the path is reported
    as incomplete (the caller raises RECOVERY_UNCERTAIN). An overwriting
    rename is never used as a restore fallback, and **no cleanup unlinks by
    name**: trash/backup entries whose content is safely back at the path (or
    deliberately reverted) are left in place and returned as ``redundant`` —
    inspect-then-remove litter, not a deletion target (round-6 finding).

    The residual window between the trash-rename and the final step is
    best-effort against uncoordinated external processes; jinlin sessions are
    serialized by the workspace write lock. See the security model
    (``docs/REPORT-AND-HANDBOOK.md`` §16).
    """
    restored: list[str] = []
    incomplete: list[str] = []
    redundant: list[str] = []
    for record in reversed(applied):
        path: Path = record["path"]
        rel: str = record["rel"]
        try:
            if record["kind"] == "add":
                if not path.exists():
                    continue
                trash = path.with_name(f".{path.name}.jinlin-rollback-{uuid.uuid4().hex[:8]}")
                os.replace(path, trash)  # content-preserving: never unlink in place
                if not record_trash(workspace, trash, rel, "rollback"):
                    manifest_degraded.append(trash.name)
                if hashlib.sha256(trash.read_bytes()).hexdigest() == record["after_sha"]:
                    redundant.append(trash.name)  # reverted content, verified ours
                    restored.append(rel)
                else:
                    if _restore_no_clobber(trash, path):
                        redundant.append(trash.name)  # content back at the path
                    # else: path claimed concurrently — the trash holds the
                    # move-time content and stays as the recoverable copy
                    incomplete.append(rel)
            elif record["kind"] == "update":
                if not path.exists():
                    incomplete.append(rel)
                    continue
                trash = path.with_name(f".{path.name}.jinlin-rollback-{uuid.uuid4().hex[:8]}")
                os.replace(path, trash)
                if not record_trash(workspace, trash, rel, "rollback"):
                    manifest_degraded.append(trash.name)
                if hashlib.sha256(trash.read_bytes()).hexdigest() != record["after_sha"]:
                    if _restore_no_clobber(trash, path):
                        redundant.append(trash.name)  # content back at the path
                    # else: path claimed concurrently — keep both sides
                    incomplete.append(rel)
                    continue
                try:
                    atomic_write(path, record["before"], create=True)
                except Exception:
                    if _restore_no_clobber(trash, path):
                        redundant.append(trash.name)  # ours is back at the path
                    # else: someone else holds the path — trash keeps our content
                    incomplete.append(rel)
                    continue
                redundant.append(trash.name)  # reverted content, verified ours
                restored.append(rel)
            else:  # delete: restore from the moved-aside backup
                if path.exists():
                    incomplete.append(rel)  # a concurrent writer recreated it
                    continue
                backup: Path = record["backup"]
                if not backup.exists():
                    incomplete.append(rel)
                    continue
                if not _restore_no_clobber(backup, path):
                    incomplete.append(rel)  # claimed concurrently, or fs cannot hard-link:
                    continue  # keep the backup, never fall back to an overwriting rename
                redundant.append(backup.name)  # content back at the path
                restored.append(rel)
        except Exception:
            incomplete.append(rel)
    return restored, incomplete, redundant


def check_clean(ctx: ToolContext, rel: str, current_sha: str) -> AgentError | None:
    """Enforce read-before-write and detect external modification."""
    state = ctx.state
    if state is None:
        return None
    seen = (state.read_hashes or {}).get(rel)
    if seen is None:
        return AgentError(
            FILE_NOT_READ,
            f"{rel} has not been read in this session; call read_file first (edits are only "
            "allowed on content the agent has actually seen)",
            source="edit",
        )
    if seen != current_sha:
        return AgentError(
            FILE_CHANGED_EXTERNALLY,
            f"{rel} changed on disk since it was read (read hash {seen[:12]}, now {current_sha[:12]}); "
            "re-read the file and retry",
            source="edit",
        )
    return None


# --------------------------------------------------------------------------- V4A parse


@dataclass
class Hunk:
    old: list[str]
    new: list[str]
    header: str = ""


@dataclass
class PatchOp:
    kind: str  # add | update | delete
    path: str
    hunks: list[Hunk] = field(default_factory=list)
    added_lines: list[str] = field(default_factory=list)


def parse_v4a(text: str) -> tuple[list[PatchOp], list[str]]:
    errors: list[str] = []
    ops: list[PatchOp] = []
    lines = text.splitlines()
    i = 0
    if not any(l.strip() == "*** Begin Patch" for l in lines):
        return [], ["missing '*** Begin Patch' line"]
    while i < len(lines) and lines[i].strip() != "*** Begin Patch":
        i += 1
    i += 1
    current: PatchOp | None = None
    current_hunk: Hunk | None = None
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped == "*** End Patch":
            break
        if stripped.startswith("*** Add File:"):
            current = PatchOp(kind="add", path=stripped.split(":", 1)[1].strip())
            ops.append(current)
            current_hunk = None
        elif stripped.startswith("*** Update File:"):
            current = PatchOp(kind="update", path=stripped.split(":", 1)[1].strip())
            ops.append(current)
            current_hunk = None
        elif stripped.startswith("*** Delete File:"):
            current = PatchOp(kind="delete", path=stripped.split(":", 1)[1].strip())
            ops.append(current)
            current_hunk = None
        elif stripped.startswith("*** Move to:"):
            errors.append("'*** Move to:' is not supported; delete and add instead")
        elif stripped.startswith("@@"):
            if current is None or current.kind != "update":
                errors.append(f"line {i + 1}: '@@' outside an Update File section")
            else:
                current_hunk = Hunk(old=[], new=[], header=stripped[2:].strip())
                current.hunks.append(current_hunk)
        else:
            if current is None:
                i += 1
                continue
            if current.kind == "add":
                if line.startswith("+"):
                    current.added_lines.append(line[1:])
                elif line == "":
                    current.added_lines.append("")
                else:
                    errors.append(f"line {i + 1}: Add File lines must start with '+'")
            elif current.kind == "update":
                if current_hunk is None:
                    current_hunk = Hunk(old=[], new=[])
                    current.hunks.append(current_hunk)
                if line.startswith("+"):
                    current_hunk.new.append(line[1:])
                elif line.startswith("-"):
                    current_hunk.old.append(line[1:])
                elif line.startswith(" "):
                    current_hunk.old.append(line[1:])
                    current_hunk.new.append(line[1:])
                elif line == "":
                    current_hunk.old.append("")
                    current_hunk.new.append("")
                else:
                    errors.append(f"line {i + 1}: hunk lines must start with ' ', '+', or '-'")
            elif current.kind == "delete":
                if line.strip():
                    errors.append(f"line {i + 1}: Delete File section must be empty")
        i += 1
    if i >= len(lines):
        errors.append("missing '*** End Patch' line")
    for op in ops:
        if op.kind == "update" and not op.hunks:
            errors.append(f"Update File {op.path} has no hunks")
        if op.kind == "update":
            for h, hunk in enumerate(op.hunks):
                if not hunk.old:
                    errors.append(
                        f"Update File {op.path} hunk {h + 1}: no context/removed lines to anchor the edit; "
                        "include at least one unchanged context line"
                    )
    return ops, errors


# --------------------------------------------------------------------------- hunk apply


def find_occurrences(lines: list[str], block: list[str]) -> list[int]:
    if not block:
        return []
    n, m = len(lines), len(block)
    return [i for i in range(0, n - m + 1) if lines[i : i + m] == block]


def apply_hunks(f: FileText, hunks: list[Hunk]) -> list[str]:
    """Return new lines; raise PATCH_CONFLICT with a diagnostic if any hunk is ambiguous."""
    placements: list[tuple[int, int, Hunk]] = []
    for idx, hunk in enumerate(hunks):
        positions = find_occurrences(f.lines, hunk.old)
        if not positions:
            raise AgentError(
                PATCH_CONFLICT,
                f"{f.rel}: hunk {idx + 1} did not match any content. "
                f"Expected {len(hunk.old)} line(s) starting with {hunk.old[0][:80]!r}. "
                "Re-read the file and copy the exact current text.",
                source="edit",
                details={"hunk": idx + 1, "path": f.rel},
            )
        if len(positions) > 1:
            raise AgentError(
                PATCH_CONFLICT,
                f"{f.rel}: hunk {idx + 1} matches {len(positions)} locations; add more context lines so it is unique.",
                source="edit",
                details={"hunk": idx + 1, "positions": positions[:10], "path": f.rel},
            )
        placements.append((positions[0], positions[0] + len(hunk.old), hunk))
    placements.sort(key=lambda t: t[0])
    for (_, prev_end, _), (nxt_start, _, _) in zip(placements, placements[1:]):
        if nxt_start < prev_end:
            raise AgentError(PATCH_CONFLICT, f"{f.rel}: overlapping hunks; split the edits", source="edit")
    new_lines = list(f.lines)
    for start, end, hunk in reversed(placements):
        new_lines[start:end] = hunk.new
    return new_lines


# --------------------------------------------------------------------------- tool handlers


def _rel_path(ctx: ToolContext, raw: str) -> tuple[Path, str]:
    p = ctx.resolve(raw)
    return p, ctx.relative(p)


def apply_patch(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        with _workspace_write_guard(ctx):
            return _apply_patch_locked(ctx, args)
    except AgentError as exc:
        return ToolResult(ok=False, error=exc)


def _apply_patch_locked(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    text = args.get("patch") or ""
    if len(text.encode("utf-8")) > int(ctx.config.workspace.get("max_patch_bytes", 2_000_000)):
        return ToolResult(ok=False, error=AgentError("FILE_TOO_LARGE", "patch too large", source="edit"))
    ops, parse_errors = parse_v4a(text)
    if parse_errors:
        return ToolResult(
            ok=False,
            error=AgentError(
                PATCH_PARSE_ERROR,
                "patch syntax error: " + "; ".join(parse_errors),
                source="edit",
                details={"expected_format": V4A_HELP},
            ),
        )
    max_bytes = int(ctx.config.workspace.get("max_file_bytes", 1_048_576))
    plan: list[dict[str, Any]] = []
    errors: list[AgentError] = []

    for op in ops:
        path, rel = _rel_path(ctx, op.path)
        if op.kind == "add":
            if path.exists():
                errors.append(AgentError(PATCH_CONFLICT, f"{rel} already exists; use Update File", source="edit"))
                continue
            content = "\n".join(op.added_lines) + ("\n" if op.added_lines else "")
            plan.append({"kind": "add", "path": path, "rel": rel, "data": content.encode("utf-8"), "old": None})
        elif op.kind == "delete":
            if not path.exists():
                errors.append(AgentError(PATCH_CONFLICT, f"{rel} does not exist", source="edit"))
                continue
            f = load_text(path, rel, max_bytes)
            err = check_clean(ctx, rel, f.sha)
            if err:
                errors.append(err)
                continue
            plan.append({"kind": "delete", "path": path, "rel": rel, "data": None, "old": f.data, "old_sha": f.sha})
        else:  # update
            if not path.exists():
                errors.append(AgentError(PATCH_CONFLICT, f"{rel} does not exist", source="edit"))
                continue
            f = load_text(path, rel, max_bytes)
            err = check_clean(ctx, rel, f.sha)
            if err:
                errors.append(err)
                continue
            try:
                new_lines = apply_hunks(f, op.hunks)
            except AgentError as exc:
                errors.append(exc)
                continue
            new_data = render(f, new_lines)
            if new_data == f.data:
                errors.append(AgentError(PATCH_CONFLICT, f"{rel}: patch produced no change", source="edit"))
                continue
            plan.append(
                {
                    "kind": "update",
                    "path": path,
                    "rel": rel,
                    "data": new_data,
                    "old": f.data,
                    "old_sha": f.sha,
                    "added": max(0, len(new_lines) - len(f.lines)),
                    "removed": max(0, len(f.lines) - len(new_lines)),
                }
            )

    if errors:
        if len(errors) == 1:
            return ToolResult(ok=False, error=errors[0])
        return ToolResult(
            ok=False,
            error=AgentError(
                PATCH_CONFLICT,
                "patch rejected: " + " | ".join(e.message for e in errors[:5]),
                source="edit",
                retriable=True,
                details={"errors": [e.to_dict() for e in errors[:5]]},
            ),
        )

    # all validated -> write (transaction: every mutation is recorded *after* it
    # succeeded, together with the hash it produced, so rollback can never touch
    # a file that a concurrent writer changed after us)
    written: list[dict[str, Any]] = []
    deleted: list[str] = []
    applied: list[dict[str, Any]] = []
    manifest_degraded: list[str] = []
    try:
        for item in plan:
            path = item["path"]
            if item["kind"] == "add":
                atomic_write(path, item["data"], create=True)
                digest = hashlib.sha256(item["data"]).hexdigest()
                applied.append({"kind": "add", "path": path, "rel": item["rel"], "after_sha": digest})
                written.append({"path": item["rel"], "hash_before": None, "hash_after": digest, "lines_added": len(item["data"].splitlines())})
            elif item["kind"] == "delete":
                expected = item["old_sha"]
                current_sha = hashlib.sha256(path.read_bytes()).hexdigest()
                if current_sha != expected:
                    raise AgentError(FILE_CHANGED_EXTERNALLY, f"{item['rel']} changed before deletion", source="edit")
                backup = path.with_name(f".{path.name}.jinlin-txn-{uuid.uuid4().hex[:8]}")
                # move the directory entry atomically; verify what we moved
                os.replace(path, backup)
                # hash and identity MUST come from the same open file object:
                # reading the hash by path and statting the path separately
                # would let a replacement that lands in between be registered
                # as our backup (round-8 finding)
                fd = os.open(backup, os.O_RDONLY | getattr(os, "O_BINARY", 0))
                try:
                    digest = hashlib.sha256()
                    while True:
                        chunk = os.read(fd, 1 << 20)
                        if not chunk:
                            break
                        digest.update(chunk)
                    moved_sha = digest.hexdigest()
                    st = os.fstat(fd)
                    backup_identity = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
                finally:
                    os.close(fd)
                if moved_sha != expected:
                    if _restore_no_clobber(backup, path):
                        raise AgentError(
                            FILE_CHANGED_EXTERNALLY,
                            f"{item['rel']} changed while being deleted; original restored",
                            source="edit",
                        )
                    raise AgentError(
                        RECOVERY_UNCERTAIN,
                        f"{item['rel']} changed while being deleted and the path was claimed "
                        f"concurrently; the original content was kept aside at {backup.name} "
                        "instead of replacing the concurrent file",
                        source="edit",
                        details={"kept_aside": backup.name},
                    )
                if not record_trash(ctx.workspace, backup, item["rel"], "txn"):
                    manifest_degraded.append(backup.name)
                applied.append(
                    {
                        "kind": "delete",
                        "path": path,
                        "rel": item["rel"],
                        "backup": backup,
                        "before_sha": expected,
                        # fingerprint of exactly the file object the hash above
                        # was computed from
                        "backup_identity": backup_identity,
                    }
                )
                deleted.append(item["rel"])
            else:
                new_sha = hashlib.sha256(item["data"]).hexdigest()
                atomic_write(path, item["data"], mode_from=path, expected_sha256=item["old_sha"])
                applied.append(
                    {
                        "kind": "update",
                        "path": path,
                        "rel": item["rel"],
                        "after_sha": new_sha,
                        "before": item["old"],
                    }
                )
                written.append(
                    {
                        "path": item["rel"],
                        "hash_before": item["old_sha"],
                        "hash_after": new_sha,
                        "lines_added": item.get("added", 0),
                        "lines_removed": item.get("removed", 0),
                    }
                )
    except Exception as exc:
        restored, incomplete, redundant = _rollback_transaction(applied, ctx.workspace, manifest_degraded)
        details = {
            "rolled_back": restored,
            "rollback_incomplete": incomplete,
            "redundant_trash": redundant,
            "trash_manifest_degraded": manifest_degraded,
        }
        if not applied and isinstance(exc, AgentError):
            # nothing was written, so there is nothing to roll back — surface
            # the precise error (e.g. PATCH_CONFLICT on a lost create race)
            # instead of a generic execution-failure wrapper
            return ToolResult(ok=False, error=exc)
        if incomplete:
            return ToolResult(
                ok=False,
                error=AgentError(
                    "RECOVERY_UNCERTAIN",
                    f"patch failed ({exc}); rollback was incomplete for: {', '.join(incomplete)}. "
                    "Those paths keep the concurrent/external content; inspect them before retrying.",
                    source="edit",
                    details=details,
                ),
                output="patch failed; partial rollback — inspect: " + ", ".join(incomplete),
            )
        return ToolResult(
            ok=False,
            error=AgentError(
                "TOOL_EXECUTION_FAILED",
                f"patch failed while writing: {exc} (all applied changes rolled back)",
                source="edit",
                details=details,
            ),
        )

    # transaction committed: drop delete backups. A committed transaction and
    # a successful cleanup are different outcomes — the delete is final and
    # never reopened here. Cleanup deletes a backup only while it still
    # provably holds the fingerprint captured at move time; anything else
    # (replaced, vanished-with-different-identity, unlink failure) is left in
    # place and reported as deferred, never turned into a tool failure.
    cleanup_deferred: list[str] = []
    for record in applied:
        if record["kind"] != "delete":
            continue
        backup: Path = record["backup"]
        identity = record.get("backup_identity")
        try:
            st = os.stat(backup)
        except FileNotFoundError:
            continue  # gone — nothing of ours left to clean
        except OSError as exc:
            # a stat failure is not "nothing to clean": keep the backup and
            # report the reason instead of silently swallowing the error
            cleanup_deferred.append({"name": backup.name, "reason": f"stat failed: {exc}"})
            continue
        provable = identity is not None and (
            st.st_dev,
            st.st_ino,
            st.st_size,
            st.st_mtime_ns,
        ) == identity
        if not provable:
            cleanup_deferred.append(
                {"name": backup.name, "reason": "current stat fingerprint does not match the moved backup"}
            )
            continue
        try:
            backup.unlink()
            forget_trash(ctx.workspace, backup)
        except OSError as exc:
            cleanup_deferred.append({"name": backup.name, "reason": f"unlink failed: {exc}"})

    read_hashes = {w["path"]: w["hash_after"] for w in written}
    summary = [f"applied {len(plan)} file operation(s):"]
    for w in written:
        summary.append(f"  M {w['path']} (+{w.get('lines_added', 0)}/-{w.get('lines_removed', 0)})")
    for d in deleted:
        summary.append(f"  D {d}")
    if manifest_degraded:
        summary.append(
            "note: trash manifest could not record: "
            + ", ".join(manifest_degraded)
            + " - these leftovers are report-only and must be removed manually"
        )
    if cleanup_deferred:
        summary.append(
            "note: backup file(s) kept after commit (ownership not provable or cleanup failed): "
            + ", ".join(d["name"] for d in cleanup_deferred)
            + " — the transaction is committed; inspect before removing"
        )
    return ToolResult(
        output="\n".join(summary),
        written_files=written,
        deleted_files=deleted,
        read_hashes=read_hashes,
        extra={
            "files": [w["path"] for w in written],
            "cleanup_deferred": cleanup_deferred,
            "trash_manifest_degraded": manifest_degraded,
        },
    )


def edit_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        with _workspace_write_guard(ctx):
            return _edit_file_locked(ctx, args)
    except AgentError as exc:
        return ToolResult(ok=False, error=exc)


def _edit_file_locked(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path, rel = _rel_path(ctx, args["path"])
    old_string = args.get("old_string") or ""
    new_string = args.get("new_string") or ""
    replace_all = bool(args.get("replace_all", False))
    max_bytes = int(ctx.config.workspace.get("max_file_bytes", 1_048_576))
    if not old_string:
        return ToolResult(ok=False, error=AgentError(PATCH_PARSE_ERROR, "old_string must not be empty", source="edit"))
    if not path.exists():
        return ToolResult(ok=False, error=AgentError(PATCH_CONFLICT, f"{rel} does not exist", source="edit"))
    f = load_text(path, rel, max_bytes)
    err = check_clean(ctx, rel, f.sha)
    if err:
        return ToolResult(ok=False, error=err)
    count = f.text.count(old_string)
    if count == 0:
        return ToolResult(
            ok=False,
            error=AgentError(
                PATCH_CONFLICT,
                f"{rel}: old_string not found. Re-read the file and copy the exact text (including "
                "indentation and line endings).",
                source="edit",
                retriable=True,
            ),
        )
    if count > 1 and not replace_all:
        return ToolResult(
            ok=False,
            error=AgentError(
                PATCH_CONFLICT,
                f"{rel}: old_string appears {count} times; include more surrounding context or set replace_all",
                source="edit",
                retriable=True,
            ),
        )
    new_text = f.text.replace(old_string, new_string, -1 if replace_all else 1)
    if new_text == f.text:
        return ToolResult(ok=False, error=AgentError(PATCH_CONFLICT, f"{rel}: edit produced no change", source="edit"))
    new_data = new_text.encode("utf-8")
    if f.had_bom:
        new_data = b"\xef\xbb\xbf" + new_data
    try:
        atomic_write(path, new_data, mode_from=path, expected_sha256=f.sha)
    except AgentError as exc:
        return ToolResult(ok=False, error=exc)
    except Exception as exc:
        return ToolResult(ok=False, error=AgentError(TOOL_EXECUTION_FAILED, str(exc), source="edit"))
    digest = hashlib.sha256(new_data).hexdigest()
    added = new_text.count("\n") - f.text.count("\n")
    return ToolResult(
        output=f"edited {rel} ({'all occurrences' if replace_all else '1 occurrence'}, delta lines {added:+d})",
        written_files=[
            {
                "path": rel,
                "hash_before": f.sha,
                "hash_after": digest,
                "lines_added": max(0, added),
                "lines_removed": max(0, -added),
            }
        ],
        read_hashes={rel: digest},
    )


def write_file(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        with _workspace_write_guard(ctx):
            return _write_file_locked(ctx, args)
    except AgentError as exc:
        return ToolResult(ok=False, error=exc)


def _write_file_locked(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    path, rel = _rel_path(ctx, args["path"])
    content = args.get("content") or ""
    create_only = bool(args.get("create_only", False))
    exists = path.exists()
    if exists and create_only:
        return ToolResult(ok=False, error=AgentError(PATCH_CONFLICT, f"{rel} exists; use apply_patch", source="edit"))
    if exists and not ctx.config.workspace.get("allow_write", True):
        return ToolResult(ok=False, error=AgentError("COMMAND_DENIED", "writes disabled", source="edit"))
    if not exists:
        path.parent.mkdir(parents=True, exist_ok=True)
    data = content.encode("utf-8")
    if exists:
        # existing files follow the same read-before-write contract as patches
        current = load_text(path, rel, int(ctx.config.workspace.get("max_file_bytes", 1_048_576)))
        error = check_clean(ctx, rel, current.sha)
        if error:
            return ToolResult(ok=False, error=error)
        try:
            atomic_write(path, data, mode_from=path, expected_sha256=current.sha)
        except AgentError as exc:
            return ToolResult(ok=False, error=exc)
        except Exception as exc:
            return ToolResult(ok=False, error=AgentError(TOOL_EXECUTION_FAILED, str(exc), source="edit"))
        before = current.sha
    else:
        try:
            atomic_write(path, data, create=True)
        except AgentError as exc:
            return ToolResult(ok=False, error=exc)
        except Exception as exc:
            return ToolResult(ok=False, error=AgentError(TOOL_EXECUTION_FAILED, str(exc), source="edit"))
        before = None
    digest = hashlib.sha256(data).hexdigest()
    return ToolResult(
        output=f"wrote {rel} ({len(data)} bytes{' ; replaced existing file after hash check' if before else ''})",
        written_files=[
            {
                "path": rel,
                "hash_before": before,
                "hash_after": digest,
            }
        ],
        read_hashes={rel: digest},
    )


V4A_HELP = """Patch format (V4A):
*** Begin Patch
*** Add File: path/new.py
+new line
*** Update File: path/existing.py
@@ optional anchor
 context line (starts with one space)
-removed line
+added line
*** Delete File: path/dead.py
*** End Patch
Rules: update hunks need at least one context or removed line; all matched lines must be
byte-identical to the current file (copy them from read_file); a patch applies to all files
atomically or to none."""


def register(registry: ToolRegistry) -> ToolRegistry:
    registry.register(
        ToolSpec(
            name="apply_patch",
            description=(
                "Edit one or more files atomically with a V4A patch. Every ' ' and '-' line must "
                "match the current file exactly (read the file first). Use this for multi-file or "
                "multi-hunk changes; use edit_file for a single exact replacement.\n" + V4A_HELP
            ),
            schema={
                "type": "object",
                "properties": {
                    "patch": {"type": "string", "description": "The full V4A patch text."},
                },
                "required": ["patch"],
                "additionalProperties": False,
            },
            handler=apply_patch,
            side_effect="WRITE",
            idempotent=False,
            group="editing",
            timeout_seconds=60,
        )
    )
    registry.register(
        ToolSpec(
            name="edit_file",
            description=(
                "Replace an exact old_string with new_string in one file. old_string must appear "
                "exactly once (unless replace_all=true) and must match the file byte-for-byte after "
                "newline normalization. Include enough surrounding context to be unambiguous."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_string": {"type": "string"},
                    "new_string": {"type": "string"},
                    "replace_all": {"type": "boolean"},
                },
                "required": ["path", "old_string", "new_string"],
                "additionalProperties": False,
            },
            handler=edit_file,
            side_effect="WRITE",
            idempotent=False,
            group="editing",
            timeout_seconds=60,
        )
    )
    registry.register(
        ToolSpec(
            name="write_file",
            description=(
                "Create a new file or fully rewrite a small existing file. Prefer apply_patch/edit_file "
                "for changes to existing code; use this for new files or generated content."
            ),
            schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                    "create_only": {"type": "boolean", "description": "Fail if the file already exists."},
                },
                "required": ["path", "content"],
                "additionalProperties": False,
            },
            handler=write_file,
            side_effect="WRITE",
            idempotent=False,
            group="editing",
            timeout_seconds=60,
        )
    )
    return registry
