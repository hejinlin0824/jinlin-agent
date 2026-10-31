"""Sandbox factory.

Never raises because of a missing L1 implementation: the harness downgrades to
L0, records the real level in `sandbox.mode`, and reports it in the final
deliverable. Honesty over marketing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .base import ProcessHandle, RunResult, Sandbox
from .null import NullSandbox

__all__ = ["Sandbox", "RunResult", "ProcessHandle", "NullSandbox", "create_sandbox"]


def create_sandbox(workspace: str | Path, config: Any) -> Sandbox:
    """Pick the strongest available sandbox for this platform/config."""
    mode = getattr(config, "sandbox", {}).get("mode", "auto")
    if mode == "off":
        return NullSandbox(str(workspace))
    if sys.platform == "win32":
        try:
            from .windows import WindowsSandbox

            return WindowsSandbox(str(workspace), config)
        except Exception as exc:  # pragma: no cover - platform dependent
            sb = NullSandbox(str(workspace))
            sb.details["downgrade_reason"] = f"windows sandbox unavailable: {exc}"
            return sb
    return NullSandbox(str(workspace))
