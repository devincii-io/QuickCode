"""Make a logged message history one a provider will accept again.

OpenAI-compatible APIs require every assistant ``tool_calls`` message to be
followed, before anything else, by exactly one tool message per call, and
refuse a tool message that answers no call. They refuse on every request, so a
history that breaks the rule once makes the conversation unusable for good.

A log can break it without anybody doing anything wrong: a headless run
cancelled between the calls and their results persists the calls alone, a
damaged line can take an assistant message with it and strand its results,
and a result can be persisted twice. None of that is fixed in the log itself
-- it is append-only -- so it is fixed on the way out, the same way every
time, which keeps a resumed prompt byte-stable across reopenings.

Loading is the only way in for such a history. A live one is paired by
construction: the loop pushes a result for every call it made, cancelled or
not, and compaction cuts only where a round begins.
"""

from __future__ import annotations

import dataclasses

from quickcode.providers.base import ChatMessage

# What the loop itself records for a call an interrupt stopped, so a resumed
# model reads the same thing a live one would have.
MISSING_RESULT = "[error] [interrupted]"


def _valid_calls(msg: ChatMessage) -> list[dict]:
    return [
        tc for tc in msg.tool_calls or []
        if isinstance(tc, dict) and isinstance(tc.get("id"), str) and tc["id"]
    ]


def repair_history(messages: list[ChatMessage]) -> list[ChatMessage]:
    """``messages`` with every tool call answered once and no stray results."""
    out: list[ChatMessage] = []
    i, n = 0, len(messages)
    while i < n:
        msg = messages[i]
        i += 1
        if msg.role == "tool":
            # Results are consumed right after the call that asked for them;
            # reaching one here means nothing asked.
            continue
        if msg.role != "assistant" or not msg.tool_calls:
            out.append(msg)
            continue
        calls = _valid_calls(msg)
        if len(calls) != len(msg.tool_calls):
            if not calls and not msg.content:
                continue
            msg = dataclasses.replace(msg, tool_calls=calls)
        out.append(msg)
        pending = {tc["id"]: str(tc.get("name") or "") for tc in calls}
        while i < n and messages[i].role == "tool":
            result = messages[i]
            i += 1
            if pending.pop(result.tool_call_id or "", None) is not None:
                out.append(result)
        for call_id, name in pending.items():
            out.append(ChatMessage(
                role="tool", content=MISSING_RESULT, tool_call_id=call_id, name=name,
            ))
    return out
