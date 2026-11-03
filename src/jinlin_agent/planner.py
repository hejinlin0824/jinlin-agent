"""Planner (v2.0 WP-V2.0-3): model-driven task decomposition with a validated
JSON schema (v2 §5.3).

The planner never executes anything — it only proposes subtask specs, which
the Supervisor validates (dependency graph, known roles) and registers. It is
a RoleSpec over the same provider protocol (ADR-001), not a separate agent.

The model answers by SUBTASK TITLE references (`depends_on`); ids are minted
here, so the model never invents identifiers. Anything malformed — bad JSON,
unknown dependency title, duplicate titles, cycles — raises
MODEL_INVALID_RESPONSE with the concrete reason; the Supervisor turns that
into an honest failure, never a silently repaired plan.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .domain import Message, new_id
from .errors import MODEL_INVALID_RESPONSE, AgentError
from .taskmodel import TaskSpec, new_task_id, validate_dependency_graph

PLANNER_SYSTEM = """You are the planner of a code-engineering agent. You decompose one goal into a
DAG of small, concrete subtasks. You never write code yourself.

Output ONLY a JSON object, no prose, no code fences:
{"subtasks": [
  {"title": "short imperative title",
   "description": "what exactly to do and where",
   "depends_on": ["title of another subtask in this list"],
   "role": "worker",
   "capabilities": ["READ"],
   "acceptance_criteria": ["machine-checkable criterion"]}
]}

Rules:
- 1 to 6 subtasks; each must be independently executable by one worker.
- every entry in depends_on MUST be the exact title of another subtask in this
  list; the dependency graph must be acyclic.
- a subtask that only investigates is READ-only; only subtasks that must edit
  files declare WRITE.
- do not restate the root goal as a subtask."""

ASSESSOR_SYSTEM = """You assess the execution strategy for one software-engineering request, given
objective repository signals. Output ONLY a JSON object:
{"strategy": "<one of: direct | plan_then_execute | parallel_workers | sequential_workers | human_input_required | refuse_or_stop>",
 "reasons": ["short reason", ...]}
Choose direct for small, clear, low-risk requests; never decompose what one
agent can do in one pass. Choose human_input_required only when the goal or
constraints are unsafe to guess."""


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1).strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise AgentError(MODEL_INVALID_RESPONSE, f"planner output contains no JSON object: {raw[:200]}", source="planner")
    try:
        data = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AgentError(MODEL_INVALID_RESPONSE, f"planner output is not valid JSON: {exc}", source="planner") from exc
    if not isinstance(data, dict):
        raise AgentError(MODEL_INVALID_RESPONSE, "planner output must be a JSON object", source="planner")
    return data


def complete_json(provider: Any, system: str, user: str) -> dict[str, Any]:
    """One structured completion call. Used by the planner and the assessor;
    these calls are task-layer decisions recorded in the task event stream,
    not session turns."""
    from .model.base import ModelRequest

    request = ModelRequest(
        request_id=new_id("req"),
        messages=[Message(role="system", content=system), Message(role="user", content=user)],
        tools=[],
        temperature=0.0,
        max_output_tokens=4096,
        stream=False,
        timeout_seconds=120,
    )
    response = provider.complete(request)
    return _extract_json(response.text)


def plan_subtasks(provider: Any, goal: str, context: str, root_id: str, instruction: str = "") -> list[TaskSpec]:
    """Ask the planner for a subtask DAG and mint validated TaskSpecs.

    `instruction` distinguishes fresh planning from bounded replanning; both
    share one schema and one validation path."""
    user_lines = [f"Goal:\n{goal}", "", f"Repository context:\n{context}"]
    if instruction:
        user_lines = [instruction, ""] + user_lines
    data = complete_json(provider, PLANNER_SYSTEM, "\n".join(user_lines))
    raw_subtasks = data.get("subtasks")
    if not isinstance(raw_subtasks, list) or not (1 <= len(raw_subtasks) <= 8):
        raise AgentError(MODEL_INVALID_RESPONSE, "planner must return 1-8 subtasks", source="planner")

    title_to_id: dict[str, str] = {}
    for item in raw_subtasks:
        if not isinstance(item, dict) or not str(item.get("title", "")).strip():
            raise AgentError(MODEL_INVALID_RESPONSE, "every subtask needs a non-empty title", source="planner")
        title = str(item["title"]).strip()
        if title in title_to_id:
            raise AgentError(MODEL_INVALID_RESPONSE, f"duplicate subtask title: {title}", source="planner")
        title_to_id[title] = new_task_id()

    specs: list[TaskSpec] = []
    for item in raw_subtasks:
        title = str(item["title"]).strip()
        deps: list[str] = []
        for dep_title in item.get("depends_on") or []:
            dep_title = str(dep_title).strip()
            if dep_title not in title_to_id:
                raise AgentError(
                    MODEL_INVALID_RESPONSE,
                    f"subtask {title!r} depends on unknown title {dep_title!r}",
                    source="planner",
                )
            deps.append(title_to_id[dep_title])
        specs.append(
            TaskSpec(
                task_id=title_to_id[title],
                root_task_id=root_id,
                parent_task_id=root_id,
                title=title,
                description=str(item.get("description", "")).strip(),
                task_kind="implementation",
                dependencies=deps,
                role=str(item.get("role", "worker")).strip() or "worker",
                capabilities=[str(c).strip().upper() for c in item.get("capabilities") or [] if str(c).strip()],
                acceptance_criteria=[str(c).strip() for c in item.get("acceptance_criteria") or [] if str(c).strip()],
            )
        )

    errors = validate_dependency_graph(specs)
    if errors:
        raise AgentError(MODEL_INVALID_RESPONSE, "planner produced an invalid DAG: " + "; ".join(errors), source="planner")
    return specs
