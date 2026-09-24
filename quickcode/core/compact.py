"""Compaction: compress a long transcript into a continuation handoff.

When the token ledger crosses ~80% of the model's context window (or on
manual /compact), we run a one-off request that summarizes the
conversation, then rebuild history as [summary seed] + the last few verbatim
turns (cut where no tool call loses its result). See docs/PROMPTS.md §4.
Inside a turn, the context guard (``core/context_guard.py``) calls the same
``run_compaction`` between two rounds.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from quickcode.core.context_size import (
    CHARS_PER_TOKEN,
    learn_window,
    message_chars,
    request_estimate,
    tokens,
    trim_tool_results,
)
from quickcode.core.events import TextDelta, TurnDone, Usage
from quickcode.prompts.compact import COMPACTION_PROMPT
from quickcode.providers.base import ChatMessage, ChatRequest, ProviderError
from quickcode.providers.overflow import is_context_overflow

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance

COMPACT_RATIO = 0.8

# The verbatim tail may fill at most this share of the context window. Two
# long turns kept whole could otherwise rebuild a history already back over
# the threshold that triggered the compaction, and it would run every turn.
TAIL_SHARE = 0.25
# A tail smaller than the floor is always worth keeping whole, whatever the
# window says.
_MIN_TAIL_CHARS = 8_000
# The summary request fills at most this share of what the window leaves after
# the reply's budget: its size is partly a chars/4 guess. The second share is
# for the one retry after the provider refused the first as too long anyway.
SUMMARY_FILL = 0.9
SUMMARY_RETRY_FILL = 0.6


def should_compact(agent: AgentInstance, ratio: float = COMPACT_RATIO) -> bool:
    """True when the context ledger has crossed the threshold."""
    pct = agent.context_pct()
    if pct is None:
        return False
    return pct >= ratio * 100.0


def _tail_budget(context_length: int | None) -> int | None:
    if not context_length:
        return None
    return max(_MIN_TAIL_CHARS, int(context_length * TAIL_SHARE * CHARS_PER_TOKEN))


def _select_tail(
    messages: list[ChatMessage], keep_turns: int, *, budget_chars: int | None = None
) -> list[ChatMessage]:
    """Keep the last ``keep_turns`` user-started turns, cut where it is safe.

    A cut lands only in front of a user or an assistant message, never in
    front of a tool result: that would orphan the result from the call that
    asked for it, and a provider refuses the request.
    """
    # ``runtime.compaction.keep_turns`` declares a minimum of 0, and 0 has to
    # mean "nothing verbatim": ``user_idxs[-0]`` is ``user_idxs[0]``, so
    # without this the smallest value a user can pick keeps the *whole*
    # transcript -- the opposite of what it says.
    if keep_turns <= 0:
        return []
    starts = [i for i, m in enumerate(messages) if m.role != "tool"]
    user_idxs = [i for i in starts if messages[i].role == "user"]
    if len(user_idxs) > keep_turns:
        cut = user_idxs[-keep_turns]
    else:
        # The whole transcript is "the last few turns" -- typically one request
        # worked for many rounds. Keeping it whole summarized nothing and grew
        # the history by the summary, so keep its last few rounds instead.
        later = [i for i in starts if i > 0]
        cut = later[-min(keep_turns, len(later))] if later else len(messages)
    if budget_chars is not None:
        size = sum(message_chars(m) for m in messages[cut:])
        for nxt in (i for i in starts if i > cut):
            if size <= budget_chars:
                break
            size -= sum(message_chars(m) for m in messages[cut:nxt])
            cut = nxt
    return messages[cut:]


async def _summarize(agent: AgentInstance) -> str:
    """Run the compaction request and return the summary text.

    It declares the same tools as the turns before it, though the prompt says
    not to call them: tools lead the cached prefix, and with none declared this
    request -- nearly a full window -- shared no cache with the conversation it
    summarizes. Its usage is emitted like any round's for the same reason: it
    is the largest request a session makes, and it was being counted nowhere.
    """
    try:
        return await _summary_request(agent, SUMMARY_FILL)
    except ProviderError as e:
        # The fit rests partly on chars/4, which code-dense text beats. Refused
        # for length, it is fitted again -- to the window the refusal names, if
        # it names one -- with a wider margin, once.
        if not is_context_overflow(str(e)):
            raise
        learn_window(agent, str(e))
        return await _summary_request(agent, SUMMARY_RETRY_FILL)


async def _summary_request(agent: AgentInstance, fill: float) -> str:
    from quickcode.core.loop import _tools_for

    messages = _fit_for_summary(agent, agent.history.build_messages(), fill)
    messages = [*messages, ChatMessage(role="user", content=COMPACTION_PROMPT)]
    req = ChatRequest(
        model=agent.model, messages=messages, tools=_tools_for(agent),
        max_tokens=getattr(agent, "max_tokens", 0) or None,
    )
    parts: list[str] = []
    async for ev in agent.provider.stream_chat(req):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
        elif isinstance(ev, Usage):
            agent.ledger.add(ev)
            agent.bus.emit(ev)
        elif isinstance(ev, TurnDone) and ev.error:
            # What streamed before the error is a summary cut short, and
            # accepting it would replace the history with half a handoff.
            raise ProviderError(ev.error)
    return "".join(parts).strip()


def _fit_for_summary(
    agent: AgentInstance, messages: list[ChatMessage], fill: float = SUMMARY_FILL
) -> list[ChatMessage]:
    """The request to summarize, cut down until it fits the window.

    The history being summarized is, by definition, nearly a window already --
    and when a turn's tool results overflowed it, more than one. A summary
    request that is refused as too long left the conversation with no way
    forward at all, ``/compact`` included. So: the oldest tool results lose
    their middle first (the handoff needs them least), then the rest; if
    what remains still does not fit, the oldest rounds are left out whole.
    Only the request is cut -- the history it summarizes is left alone.
    """
    window = agent.context_length
    if not window:
        return messages
    reserve = min(getattr(agent, "max_tokens", 0) or 0, window // 4)
    budget = int((window - reserve) * fill)
    over = request_estimate(agent)[0] + tokens(len(COMPACTION_PROMPT)) - budget
    if over <= 0:
        return messages
    need = over * CHARS_PER_TOKEN
    out = list(messages)
    for oldest_first in (True, False):
        need -= trim_tool_results(out, need, oldest_first=oldest_first)[1]
        if need <= 0:
            return out
    # Left out from the front, a round at a time. A cut lands only where a
    # round begins, so no result loses its call; the seed of an earlier
    # compaction stays, since it is the only record of what came before it.
    # Not when the history is not what is over: dropping it would lose the
    # conversation and still not fit.
    system, body = out[0], out[1:]
    if need >= sum(message_chars(m) for m in body):
        return out
    first = 1 if body and body[0].content.startswith("<compaction-summary>") else 0
    starts = [i for i, m in enumerate(body) if i > first and m.role != "tool"]
    cut = first
    for nxt in starts[:-1]:
        if need <= 0:
            break
        need -= sum(message_chars(m) for m in body[cut:nxt])
        cut = nxt
    return [system, *body[:first], *body[cut:]]


async def run_compaction(agent: AgentInstance, *, keep_turns: int = 2) -> str:
    """Summarize and rebuild history in place. Returns the summary.

    Raises ProviderError if the summarization request fails; the caller should
    surface it and leave history untouched (this function only mutates history
    after a successful summary).
    """
    summary = await _summarize(agent)
    if not summary:
        raise ProviderError("compaction produced an empty summary")
    tail = _select_tail(
        agent.history.messages, keep_turns,
        budget_chars=_tail_budget(getattr(agent, "context_length", None)),
    )
    agent.history.replace_with_summary(summary, tail)
    agent.mark_compacted()
    # Drop the context footprint: history was just rebuilt, so the last
    # request's size describes a transcript that no longer exists, and the
    # meter would sit pinned at the threshold that triggered this until the
    # next request re-measures. The cumulative fields are *not* touched --
    # compacting a conversation does not un-spend what it already cost, and
    # zeroing them here made the status bar and a resumed session (which
    # rebuilds from the log) disagree about the same conversation.
    agent.ledger.last_input_tokens = 0
    agent.ledger.last_output_tokens = 0
    return summary
