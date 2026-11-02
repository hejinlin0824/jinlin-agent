"""Run the focused Rescue fault-injection, crash, and seeded-chaos matrix."""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/test_rescue_fault_matrix.py",
        "tests/test_rescue_crash_recovery.py",
        "tests/test_rescue_chaos.py",
    ]
    print("========== Rescue fault-injection + crash + seeded-chaos matrix ==========", flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        print(f"[FAIL] Rescue fault/chaos matrix (exit {completed.returncode})", flush=True)
        return completed.returncode
    print("[PASS] Rescue fault-injection + crash + seeded-chaos matrix", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
