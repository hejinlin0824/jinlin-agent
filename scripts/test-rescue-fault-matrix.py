"""Run the focused deterministic Rescue fault-injection matrix."""
from __future__ import annotations

import subprocess
import sys


def main() -> int:
    command = [sys.executable, "-m", "pytest", "-q", "tests/test_rescue_fault_matrix.py", "tests/test_rescue_crash_recovery.py"]
    print("========== Rescue fault-injection matrix ==========", flush=True)
    completed = subprocess.run(command, check=False)
    if completed.returncode:
        print(f"[FAIL] Rescue fault-injection matrix (exit {completed.returncode})")
        return completed.returncode
    print("[PASS] Rescue fault-injection matrix")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
