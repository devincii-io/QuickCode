"""Compaction: compress a long transcript into a continuation handoff.

When the token ledger crosses ~80% of the model's context window (or on
manual /compact), we run a one-off request that summarizes the
conversation, then rebuild history as [summary seed] + the last few verbatim
turns (cut where no tool call loses its result). See docs/PROMPTS.md §4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from quickcode.core.events import TextDelta, TurnDone, Usage
from quickcode.prompts.compact import COMPACTION_PROMPT
from quickcode.providers.base import ChatMessage, ChatRequest, ProviderError

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance

COMPACT_RATIO = 0.8

# The verbatim tail may fill at most this share of the context window. Two
# long turns kept whole could otherwise rebuild a history already back over
# the threshold that triggered the compaction, and it would run every turn.
TAIL_SHARE = 0.25
# Rough, and only used for that cap. A tail smaller than the floor is always
# worth keeping whole, whatever the window says.
_CHARS_PER_TOKEN = 4
_MIN_TAIL_CHARS = 8_000


def should_compact(agent: AgentInstance, ratio: float = COMPACT_RATIO) -> bool:
    """True when the context ledger has crossed the threshold."""
    pct = agent.context_pct()
    if pct is None:
        return False
    return pct >= ratio * 100.0


def _size(m: ChatMessage) -> int:
    return len(m.content or "") + sum(
        len(str(tc.get("arguments", ""))) for tc in m.tool_calls
    )


def _tail_budget(context_length: int | None) -> int | None:
    if not context_length:
        return None
    return max(_MIN_TAIL_CHARS, int(context_length * TAIL_SHARE * _CHARS_PER_TOKEN))


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
        size = sum(_size(m) for m in messages[cut:])
        for nxt in (i for i in starts if i > cut):
            if size <= budget_chars:
                break
            size -= sum(_size(m) for m in messages[cut:nxt])
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
    from quickcode.core.loop import _tools_for

    messages = agent.history.build_messages()  # [system, *history]
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
