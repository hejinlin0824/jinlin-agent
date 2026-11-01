"""Trusted path resolution — the single workspace-boundary implementation.

Every filesystem entry point (file tools, edit engine, shell cwd) goes through
`resolve_in_workspace`. It resolves the *real* path, not just the string prefix:
a workspace-local symlink or Windows junction pointing outside is rejected even
when the final target does not exist yet.

Limitations are stated in the review response: this closes the static escape and
the parent-link escape; fully race-free operation would require directory-handle
based APIs (documented as future work).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .errors import PATH_OUTSIDE_WORKSPACE, AgentError

_REPARSE_ATTR = 0x400  # FILE_ATTRIBUTE_REPARSE_POINT


def _is_link_like(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
    except OSError:
        return False
    if sys.platform == "win32":
        try:
            if hasattr(os.path, "isjunction") and os.path.isjunction(path):
                return True
            attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            return bool(attrs & _REPARSE_ATTR)
        except (OSError, AttributeError):
            return False
    return False


def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def within(root: Path, path: Path) -> bool:
    root_s = _norm(str(root))
    target = _norm(str(path))
    return target == root_s or target.startswith(root_s.rstrip(os.sep) + os.sep)


def escape_error(raw: str, detail: str = "") -> AgentError:
    message = f"path escapes the workspace: {raw}"
    if detail:
        message += f" ({detail})"
    return AgentError(PATH_OUTSIDE_WORKSPACE, message, source="policy")


def resolve_in_workspace(workspace: str | Path, raw: str, follow_symlinks: bool = False) -> Path:
    """Resolve `raw` and guarantee the real location is inside `workspace`."""
    root = Path(os.path.abspath(str(workspace)))
    candidate = Path(str(raw or "."))
    if candidate.is_absolute():
        path = Path(os.path.abspath(candidate))
    else:
        path = Path(os.path.abspath(root / candidate))

    # 1) lexical boundary (fast rejection for ../ and absolute escapes)
    if not within(root, path):
        raise escape_error(str(raw))

    # 2) real boundary: resolves symlinks AND junctions, including a non-existent
    #    leaf whose existing parent chain leaves the workspace
    real = Path(os.path.realpath(str(path)))
    if not within(root, real) and not within(Path(os.path.realpath(str(root))), real):
        raise escape_error(str(raw), "resolved outside via link/junction")

    # 3) explicit link components when symlink following is disabled
    if not follow_symlinks:
        current = path
        while True:
            if _is_link_like(current):
                raise escape_error(str(raw), f"link component: {current.name}")
            if _norm(str(current)) == _norm(str(root)) or current.parent == current:
                break
            current = current.parent
    return path
