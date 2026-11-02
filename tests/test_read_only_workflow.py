from types import SimpleNamespace

from jinlin_agent.domain import SideEffect
from jinlin_agent.prompt import build_system_prompt
from jinlin_agent.session import workspace_snapshot
from jinlin_agent.tools.base import ToolSpec
from jinlin_agent.tools.registry import ToolRegistry


def _spec(name, side_effect):
    return ToolSpec(
        name=name,
        description=name,
        schema={"type": "object", "properties": {}, "additionalProperties": False},
        handler=lambda _ctx, _args: None,
        side_effect=side_effect,
    )


def test_read_only_tool_catalog_excludes_commands_and_workflow_ceremony():
    registry = ToolRegistry()
    registry.register(_spec("read_file", SideEffect.READ.value))
    registry.register(_spec("run_command", SideEffect.EXECUTE.value))
    registry.register(_spec("apply_patch", SideEffect.WRITE.value))
    registry.register(_spec("update_plan", SideEffect.NONE.value))
    registry.register(_spec("propose_completion", SideEffect.NONE.value))
    registry.register(_spec("request_user_input", SideEffect.NONE.value))

    assert [item["function"]["name"] for item in registry.definitions(read_only=True)] == [
        "read_file",
        "request_user_input",
    ]
    assert len(registry.definitions()) == 6


def test_read_only_prompt_does_not_require_plan_completion_or_verification():
    config = SimpleNamespace(
        workspace={"allow_write": True},
        verification={"require_for_code_changes": True},
        workspace_root=".",
    )
    state = SimpleNamespace(workspace=".")
    prompt = build_system_prompt(config, state, read_only=True)

    assert "Start with get_project_summary" in prompt
    assert "Never call run_command" in prompt
    assert "not applicable to this read-only analysis turn" in prompt
    assert "call update_plan" not in prompt
    assert "call propose_completion" not in prompt


def test_read_only_workspace_snapshot_does_not_scan_project_or_spawn_git(tmp_path):
    class NoCommandSandbox:
        def run(self, *_args, **_kwargs):
            raise AssertionError("read-only session startup must not spawn git commands")

    snapshot = workspace_snapshot(
        tmp_path,
        SimpleNamespace(),
        NoCommandSandbox(),
        include_project=False,
        include_git=False,
    )

    assert snapshot["workspace_root"] == str(tmp_path)
    assert snapshot["project_types"] == []
    assert snapshot["test_commands"] == []
    assert snapshot["git_repo"] is False
