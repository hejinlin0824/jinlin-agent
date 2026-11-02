from __future__ import annotations

import pytest

from jinlin_agent.errors import AgentError
from jinlin_agent.model.openai_compat import _decode_provider_bytes


def test_provider_bytes_decode_utf8_without_replacement():
    assert _decode_provider_bytes("项目分析完成".encode("utf-8")) == "项目分析完成"


def test_provider_invalid_utf8_is_reported_instead_of_silently_garbled():
    with pytest.raises(AgentError) as exc:
        _decode_provider_bytes(b"\xff\xfe\xfa")
    assert exc.value.code == "MODEL_INVALID_RESPONSE"
    assert "not valid UTF-8" in exc.value.message
