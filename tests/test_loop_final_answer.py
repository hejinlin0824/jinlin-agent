from types import SimpleNamespace

from jinlin_agent.loop import Harness


class _Session:
    def __init__(self):
        self.events = []

    def emit(self, event_type, payload):
        self.events.append((event_type, payload))


def _harness_with_state(*, written_files=None):
    harness = Harness.__new__(Harness)
    harness.state = SimpleNamespace(
        written_files=written_files or {},
        deleted_files=[],
    )
    harness.session = _Session()
    harness.history = []
    harness.invalid_decisions = 0
    harness.progress_callback = lambda _message: None
    return harness


def test_plain_text_answer_completes_read_only_session_without_more_tool_rounds():
    harness = _harness_with_state()
    response = SimpleNamespace(text="# 项目结构\n\n这是最终分析。", finish_reason="stop")

    result = harness._handle_text_only(response)

    assert result == "terminal"
    assert harness.session.events == [
        (
            "session.completed",
            {
                "result": "completed",
                "summary": "# 项目结构\n\n这是最终分析。",
                "report_artifact_id": None,
                "warnings": ["Answered without modifying workspace files."],
                "evidence": [],
            },
        )
    ]
    assert harness.history == []


def test_text_only_response_after_workspace_changes_still_requires_completion_gate():
    harness = _harness_with_state(written_files={"src/app.py": {"hash_after": "abc"}})
    response = SimpleNamespace(text="代码已经修改。", finish_reason="stop")

    result = harness._handle_text_only(response)

    assert result == "continue"
    assert harness.session.events == []
    assert harness.history
    assert "propose_completion" in harness.history[-1].content
