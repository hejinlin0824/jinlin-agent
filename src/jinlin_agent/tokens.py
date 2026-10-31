"""Token estimation without a tokenizer.

The harness must budget context even for models whose tokenizer we do not ship.
A conservative heuristic: CJK characters ~1 token each, latin text ~4 chars per
token, plus per-message overhead. It is only used for budgeting/compaction
decisions; the provider reports real usage which is what metrics use.
"""

from __future__ import annotations

_MESSAGE_OVERHEAD = 6


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    cjk = 0
    other = 0
    for ch in text:
        if ord(ch) > 0x2E80:  # CJK ideographs, kana, hangul, full-width forms
            cjk += 1
        else:
            other += 1
    return int(cjk + other / 4) + 1


def estimate_messages_tokens(messages: list[object]) -> int:
    total = 0
    for m in messages:
        if isinstance(m, str):
            total += estimate_tokens(m) + _MESSAGE_OVERHEAD
            continue
        content = getattr(m, "content", "") or ""
        total += estimate_tokens(content) + _MESSAGE_OVERHEAD
        for tc in getattr(m, "tool_calls", []) or []:
            total += estimate_tokens(getattr(tc, "name", "")) + estimate_tokens(
                getattr(tc, "arguments", "")
            )
    return total
