"""L0 sandbox: plain subprocess, no OS containment.

Still kills the process tree on timeout (best effort) so a runaway command does
not leak children, but it does not restrict filesystem writes. The engine name
in every tool event says exactly this.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import Any

from .base import ProcessHandle, RunResult, Sandbox, process_env

_IS_WIN = os.name == "nt"


def _kill_tree(pid: int) -> None:
    if _IS_WIN:
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except Exception:
            pass
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except Exception:
            try:
                os.kill(pid, signal.SIGKILL)
            except Exception:
                pass


class _NullHandle:
    def __init__(self, proc: subprocess.Popen) -> None:
        self._proc = proc
        self.pid = proc.pid
        self._out = ""
        self._err = ""
        self._lock = threading.Lock()
        self._closed = False

    def poll(self) -> int | None:
        return self._proc.poll()

    def wait(self, timeout: float | None = None) -> int | None:
        try:
            return self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def read_available(self) -> tuple[str, str]:
        with self._lock:
            out, err = self._out, self._err
            self._out, self._err = "", ""
        return out, err

    def _pump(self) -> None:
        try:
            if self._proc.stdout is not None:
                for line in self._proc.stdout:
                    with self._lock:
                        self._out += line
            if self._proc.stderr is not None:
                for line in self._proc.stderr:
                    with self._lock:
                        self._err += line
        except Exception:
            pass

    def start_pump(self) -> None:
        threading.Thread(target=self._pump, daemon=True).start()

    def terminate_tree(self) -> None:
        if self.pid:
            _kill_tree(self.pid)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._proc.stdout and self._proc.stdout.close()
                self._proc.stderr and self._proc.stderr.close()
            except Exception:
                pass


class NullSandbox(Sandbox):
    level = "L0"
    engine = "subprocess"

    def run(
        self,
        argv: list[str],
        cwd: str,
        timeout: float,
        env: dict[str, str] | None = None,
        stdin_data: str | None = None,
        max_output_chars: int = 200_000,
    ) -> RunResult:
        t0 = time.monotonic()
        merged = dict(os.environ)
        merged.update(process_env(self.workspace))
        if env:
            merged.update(env)
        kwargs: dict[str, Any] = {}
        if _IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=merged,
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **kwargs,
        )
        timed_out = False
        try:
            out, err = proc.communicate(input=stdin_data, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(proc.pid)
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:
                out, err = "", ""
        duration = int((time.monotonic() - t0) * 1000)
        out = out or ""
        err = err or ""
        t_out = len(out) > max_output_chars
        t_err = len(err) > max_output_chars
        return RunResult(
            exit_code=proc.returncode,
            stdout=out[:max_output_chars],
            stderr=err[:max_output_chars],
            timed_out=timed_out,
            duration_ms=duration,
            engine=self.engine,
            pid=proc.pid,
            truncated_stdout=t_out,
            truncated_stderr=t_err,
        )

    def popen(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str] | None = None,
    ) -> ProcessHandle:
        merged = dict(os.environ)
        merged.update(process_env(self.workspace))
        if env:
            merged.update(env)
        kwargs: dict[str, Any] = {}
        if _IS_WIN:
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=merged,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **kwargs,
        )
        handle = _NullHandle(proc)
        handle.start_pump()
        return handle
