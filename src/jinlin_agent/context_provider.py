"""Context providers (v2.0 WP-V2.0-7; v2 §8.2/§8.3).

A ContextProvider supplies scored, budgeted context blocks. DependencyContext-
Provider injects the recorded deliverables of a task's completed dependencies,
so a worker never has to re-derive what upstream already produced. Blocks are
ordinary optional candidates: under budget pressure they are dropped WITH a
recorded reason (never silently), and required blocks always win.
"""

from __future__ import annotations

import json
from typing import Any

from .context import ContextBlock


class DependencyContextProvider:
    def __init__(self, ledger: Any, dependencies: list[str]) -> None:
        self.ledger = ledger
        self.dependencies = [d for d in dependencies if str(d).strip()]

    def blocks(self, state: Any, budget: int) -> list[ContextBlock]:
        out: list[ContextBlock] = []
        for dep_id in self.dependencies:
            row = self.ledger.get_task(dep_id)
            if not row or row.get("status") != "completed":
                continue
            result = row.get("result")
            if not isinstance(result, dict):
                continue
            body = json.dumps(result, ensure_ascii=False)[:4000]
            out.append(
                ContextBlock(
                    block_id=f"dependency:{row.get('title') or dep_id}",
                    source="dependency",
                    content=f"## Deliverable of upstream task '{row.get('title') or dep_id}'\n{body}",
                    priority=890,
                    score=88,
                    reason="dependency deliverable (v2 §8.3: directly upstream results rank above project notes)",
                )
            )
        return out
