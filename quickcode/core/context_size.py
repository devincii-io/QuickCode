"""How big a request is, and cutting tool results down to fit.

The only real measurement of a request is the provider's own ``usage``, which
the ledger keeps for the last one. Everything else here is characters divided
by ``CHARS_PER_TOKEN``: rough, cheap, and only ever used for the part of a
request nobody has measured yet.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from quickcode.providers.base import ChatMessage
from quickcode.providers.overflow import overflow_limit

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance

CHARS_PER_TOKEN = 4
# What a cut tool result keeps, head and tail together, when nothing says how
# much room is needed.
KEEP_CHARS = 2_000
# The least a cut result keeps. Below this it is a marker with nothing round it.
MIN_KEEP_CHARS = 400
# More than the marker a cut adds: a result within this of its cap is left
# alone, so one already cut to that cap is not cut again.
_SLACK = 400


def tokens(chars: int) -> int:
    return -(-chars // CHARS_PER_TOKEN)


def message_chars(m: ChatMessage) -> int:
    return len(m.content or "") + sum(len(str(tc.get("arguments", ""))) for tc in m.tool_calls)


def request_chars(agent: AgentInstance) -> int:
    """Every character the next request carries: system, tools, history."""
    from quickcode.core.loop import _tools_for

    tools = sum(
        len(t.name) + len(t.description) + len(json.dumps(t.parameters, ensure_ascii=False))
        for t in _tools_for(agent)
    )
    history = agent.history
    return len(history.system_prompt) + tools + sum(message_chars(m) for m in history.messages)


def request_estimate(agent: AgentInstance) -> tuple[int, bool]:
    """Tokens the next request will carry, and whether that rests on a measurement.

    Measured: the ledger's last request plus the reply to it -- which is the
    last assistant message in history -- and chars/4 for everything appended
    after that reply (the round's tool results, a user message, reminders).
    With nothing measured (a fresh session, a provider that reports no usage,
    the first request after a compaction) it is chars/4 of the whole request.
    """
    ledger = agent.ledger
    msgs = agent.history.messages
    if ledger.last_input_tokens > 0:
        start = next(
            (i + 1 for i in range(len(msgs) - 1, -1, -1) if msgs[i].role == "assistant"), 0
        )
        since = sum(message_chars(m) for m in msgs[start:])
        return ledger.last_input_tokens + ledger.last_output_tokens + tokens(since), True
    return tokens(request_chars(agent)), False


def learn_window(agent: AgentInstance, error: str) -> None:
    """Take the context window from a refusal that names it, if it is news.

    A subagent on a model the catalog did not list, or ``-p`` before its
    catalog arrived, has no window at all; the provider just said what it is.
    """
    limit = overflow_limit(error)
    if limit and (not agent.context_length or limit < agent.context_length):
        agent.context_length = limit


def cut(text: str, keep: int = KEEP_CHARS) -> str:
    """The head and tail of ``text`` with a marker saying what went, and why."""
    if len(text) <= keep:
        return text
    head = keep // 2
    tail = keep - head
    return (
        f'{text[:head]}\n<truncated total="{len(text)}" hint="middle cut to fit the context '
        f'window; fetch the part you need with a narrower call"/>\n{text[-tail:]}'
    )


def trim_tool_results(
    messages: list[ChatMessage], need_chars: int | None, *, oldest_first: bool = False
) -> tuple[int, int]:
    """Cut tool results down to head + tail, in ``messages``, to free ``need_chars``.

    By default every result is held to one cap: the highest that frees
    ``need_chars``, never below ``MIN_KEEP_CHARS`` -- so one giant result is
    all that goes when it alone is the problem, and a turn of many middling
    ones gives up a little of each. With ``need_chars`` None (nobody knows the
    window) the cap is ``KEEP_CHARS``. Largest first, and newest first among
    equals, which leaves the cached prefix alone where it can.

    ``oldest_first`` is the order for a summary request instead: results are
    cut to ``KEEP_CHARS`` from the oldest on, since the oldest output matters
    least to a handoff.

    Cut messages are replaced, never edited, so a list holding the originals
    (a snapshot on its way to the session log) keeps them. Returns
    ``(results cut, chars freed)``.
    """
    tool = [i for i, m in enumerate(messages) if m.role == "tool"]
    if oldest_first or need_chars is None:
        keep = KEEP_CHARS
    else:
        keep = _cap([len(messages[i].content) for i in tool], need_chars)
    idxs = [i for i in tool if len(messages[i].content) > keep + _SLACK]
    if not oldest_first:
        idxs.sort(key=lambda i: (len(messages[i].content), i), reverse=True)
    count = freed = 0
    for i in idxs:
        if need_chars is not None and freed >= need_chars:
            break
        before = messages[i].content
        after = cut(before, keep)
        messages[i] = replace(messages[i], content=after)
        freed += len(before) - len(after)
        count += 1
    return count, freed


def _cap(lengths: list[int], need: int) -> int:
    """The highest length every result can be held to that still frees ``need``."""

    def freed(cap: int) -> int:
        return sum(n - cap - _SLACK for n in lengths if n > cap + _SLACK)

    lo, hi = MIN_KEEP_CHARS, max(lengths, default=MIN_KEEP_CHARS)
    if freed(lo) < need:
        return lo
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if freed(mid) >= need:
            lo = mid
        else:
            hi = mid - 1
    return lo
