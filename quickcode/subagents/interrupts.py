"""A child cut off mid-turn: its history repaired, and what it had said.

The main agent is stopped through its cancel flag, and the loop writes an
``[interrupted]`` result for every call it had in flight before returning. A
child is stopped by ``CancelledError`` instead -- the parent's interrupt
cancels the gather the child runs in, and a job's cancel cancels its task --
and that unwinds the loop past the point where results are pushed. What is
left is an assistant message whose tool calls nothing answers, and an
OpenAI-compatible provider refuses every later request that carries one, so
the child could never be resumed again.
"""

from __future__ import annotations

from quickcode.core.events import AssembledToolCall
from quickcode.core.history import History

INTERRUPTED = "[interrupted]"


def close_unanswered_calls(history: History) -> int:
    """Answer the last round's orphaned calls the way the loop would have.

    Returns how many were answered. Only the last assistant message can have
    any: every earlier round was answered before the next one was sent.
    """
    messages = history.messages
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].role == "assistant":
            break
    else:
        return 0
    calls = messages[index].tool_calls or []
    tail = messages[index + 1:]
    answered = {m.tool_call_id for m in tail if m.role == "tool"}
    missing = [c for c in calls if c.get("id") not in answered]
    if not missing or any(m.role != "tool" for m in tail):
        return 0
    history.push_tool_results([
        (AssembledToolCall(id=c["id"], name=c.get("name", ""),
                           arguments=c.get("arguments", "")), INTERRUPTED, True)
        for c in missing
    ])
    return len(missing)


def partial_output(history: History) -> str:
    """What a child said in its current turn before it stopped, oldest first.

    Every round's text, not only the last: a research child's findings are
    spread across the rounds that led up to its tool calls.
    """
    texts: list[str] = []
    for message in reversed(history.messages):
        if message.role == "user":
            break
        if message.role == "assistant" and (message.content or "").strip():
            texts.append(message.content.strip())
    return "\n\n".join(reversed(texts))
