"""Sandbox contract.

Two levels:
  L0 — policy + approval + audit only. No OS containment. Reported honestly.
  L1 — OS containment: child processes run in a Job Object (process-tree kill,
       timeout), write access outside the workspace is blocked by a restricted
       token (WRITE_RESTRICTED) plus a low integrity level when available.

The harness never claims more containment than `engine`/`level` says.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class RunResult:
    exit_code: int | None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    duration_ms: int = 0
    engine: str = "none"
    pid: int | None = None
    error: str | None = None
    truncated_stdout: bool = False
    truncated_stderr: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class ProcessHandle(Protocol):
    pid: int | None

    def poll(self) -> int | None: ...

    def wait(self, timeout: float | None = None) -> int | None: ...

    def read_available(self) -> tuple[str, str]: ...

    def terminate_tree(self) -> None: ...

    def close(self) -> None: ...


class Sandbox:
    level: str = "L0"
    engine: str = "none"

    def __init__(self, workspace: str) -> None:
        self.workspace = workspace
        self.details: dict[str, Any] = {}

    # ------------------------------------------------------------------ api

    def run(
        self,
        argv: list[str],
        cwd: str,
        timeout: float,
        env: dict[str, str] | None = None,
        stdin_data: str | None = None,
        max_output_chars: int = 200_000,
    ) -> RunResult:
        raise NotImplementedError

    def popen(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> ProcessHandle:
        raise NotImplementedError

    def probe_write(self, path: str) -> tuple[bool, str]:
        """Best-effort check whether the sandbox would allow writing `path`."""
        return True, "no probe available"

    def close(self) -> None:
        pass

    # ------------------------------------------------------------------ util

    def describe(self) -> dict[str, Any]:
        return {"level": self.level, "engine": self.engine, "workspace": self.workspace, **self.details}


class Timer:
    def __init__(self) -> None:
        self._t0 = time.monotonic()

    @property
    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)


def process_env(workspace: str) -> dict[str, str]:
    """Environment for sandboxed child processes.

    Encoding is forced to UTF-8 (Chinese Windows would otherwise hand us cp936
    bytes), and TMP/TEMP are redirected inside the workspace: an L1 sandbox
    cannot write the system temp directory, so tools that use tempfile (pytest,
    installers, many libraries) would otherwise fail for an unrelated reason.
    """
    tmp = os.path.join(str(workspace), ".jinlin", "tmp")
    try:
        os.makedirs(tmp, exist_ok=True)
    except OSError:
        tmp = str(workspace)
    return {
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8:replace",
        "TMP": tmp,
        "TEMP": tmp,
        "TMPDIR": tmp,
    }
