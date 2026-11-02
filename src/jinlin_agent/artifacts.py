"""Content-addressed artifact store per session.

Large tool outputs, raw model responses, workspace snapshots, diffs, verification
logs and the final report all land here; the model only ever sees a truncated
preview plus an artifact reference.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class Artifact:
    artifact_id: str
    kind: str
    path: str
    sha256: str
    size: int
    created_at: str
    meta: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class ArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._index_path = self.root / "index.json"
        self._index: dict[str, Artifact] = {}
        self._save_lock = threading.RLock()
        if self._index_path.exists():
            try:
                raw = json.loads(self._index_path.read_text(encoding="utf-8"))
                self._index = {k: Artifact(**v) for k, v in raw.items()}
            except Exception:
                self._index = {}

    # ------------------------------------------------------------------ write

    def write(self, kind: str, content: str | bytes, meta: dict[str, Any] | None = None) -> Artifact:
        data = content.encode("utf-8") if isinstance(content, str) else content
        sha = hashlib.sha256(data).hexdigest()
        artifact_id = f"artifact_{sha[:12]}_{int(time.time() * 1000) % 100000:05d}"
        path = self.root / artifact_id
        with open(path, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        art = Artifact(
            artifact_id=artifact_id,
            kind=kind,
            path=str(path),
            sha256=sha,
            size=len(data),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            meta=meta or {},
        )
        self._index[artifact_id] = art
        self._save_index()
        return art

    def write_json(self, kind: str, obj: Any, meta: dict[str, Any] | None = None) -> Artifact:
        return self.write(kind, json.dumps(obj, ensure_ascii=False, indent=2), meta)

    # ------------------------------------------------------------------ read

    def read(self, artifact_id: str) -> str:
        art = self._index.get(artifact_id)
        if art is None:
            raise KeyError(f"unknown artifact: {artifact_id}")
        return Path(art.path).read_text(encoding="utf-8", errors="replace")

    def get(self, artifact_id: str) -> Artifact | None:
        return self._index.get(artifact_id)

    def exists(self, artifact_id: str) -> bool:
        art = self._index.get(artifact_id)
        return bool(art and Path(art.path).exists())

    def list(self, kind: str | None = None) -> list[Artifact]:
        items = list(self._index.values())
        if kind:
            items = [a for a in items if a.kind == kind]
        return items

    def _save_index(self) -> None:
        """Persist the index atomically with a unique same-directory temp file.

        A fixed index.tmp name lets concurrent writers collide. Windows may
        also briefly deny a destination rename while an antivirus or indexer
        has the file open. Flush the temp file, then retry only transient
        Windows access/sharing errors. Persistent failures still propagate.
        """
        with self._save_lock:
            payload = json.dumps(
                {k: v.to_dict() for k, v in self._index.items()},
                ensure_ascii=False,
            )
            fd, tmp_name = tempfile.mkstemp(
                prefix=".index-",
                suffix=".tmp",
                dir=str(self.root),
            )
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                    fh.write(payload)
                    fh.flush()
                    os.fsync(fh.fileno())

                delays = (0.02, 0.05, 0.1, 0.2)
                for attempt in range(len(delays) + 1):
                    try:
                        os.replace(tmp, self._index_path)
                        break
                    except PermissionError as exc:
                        if getattr(exc, "winerror", None) not in (5, 32, 33):
                            raise
                        if attempt >= len(delays):
                            raise
                        time.sleep(delays[attempt])
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
