from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path

from jinlin_agent.trash import clean_rescue, inspect_rescue, record_trash, restore_rescue


def run_cli(*args: str) -> str:
    completed = subprocess.run(
        [sys.executable, "-m", "jinlin_agent", *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"CLI failed ({completed.returncode}): {' '.join(args)}\n{completed.stdout}"
        )
    return completed.stdout


def main() -> int:
    payload = b"Jinlin Agent isolated rescue smoke test\n"
    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="jinlin-rescue-smoke-") as raw:
        workspace = Path(raw)
        rescue = workspace / ".smoke.txt.jinlin-rescue-a1b2c3d4"
        rescue.write_bytes(payload)
        if not record_trash(workspace, rescue, "smoke.txt", "rescue"):
            raise RuntimeError("could not write rescue manifest record")

        listing = run_cli("rescue", "list", "--workspace", str(workspace))
        if "verified rescue files: 1" not in listing:
            raise RuntimeError(f"rescue list did not verify fixture:\n{listing}")

        restored = restore_rescue(workspace, rescue.name)
        if restored["status"] != "restored":
            raise RuntimeError(f"restore failed: {restored}")
        target = workspace / "smoke.txt"
        if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise RuntimeError("restored target bytes do not match source")

        inspect_output = run_cli("rescue", "inspect", "--workspace", str(workspace))
        if "PUBLISHED" not in inspect_output or "audit_events:" not in inspect_output:
            raise RuntimeError(f"inspect did not report published transaction:\n{inspect_output}")

        preview = clean_rescue(workspace)
        if preview["status"] != "dry-run" or len(preview["eligible"]) != 1:
            raise RuntimeError(f"cleanup preview unexpected: {preview}")
        if not rescue.exists():
            raise RuntimeError("dry-run unexpectedly removed Rescue evidence")

        applied = clean_rescue(workspace, apply=True)
        if applied["removed"] != [rescue.name]:
            raise RuntimeError(f"controlled cleanup failed: {applied}")
        if rescue.exists() or not target.exists() or target.read_bytes() != payload:
            raise RuntimeError("cleanup removed target or failed to remove verified evidence")

        final = inspect_rescue(workspace)
        if final["status"] != "ok":
            raise RuntimeError(f"final inspection requires attention: {final}")
        if final["transactions"][0]["state"] != "CLEANED":
            raise RuntimeError(f"transaction did not reach CLEANED: {final['transactions']}")

    print("[PASS] rescue list -> restore -> audit/inspect -> clean preview -> clean apply")
    print("[PASS] isolated temporary workspace; no project files modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
