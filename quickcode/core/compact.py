"""Compaction: compress a long transcript into a continuation handoff.

When the token ledger crosses ~80% of the model's context window (or on
manual /compact), we run a one-off no-tools request that summarizes the
conversation, then rebuild history as [summary seed] + the last few verbatim
turns (cut at a user-message boundary). See docs/PROMPTS.md §4.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from quickcode.core.events import TextDelta
from quickcode.prompts.compact import COMPACTION_PROMPT
from quickcode.providers.base import ChatMessage, ChatRequest, ProviderError

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance

COMPACT_RATIO = 0.8


def should_compact(agent: AgentInstance, ratio: float = COMPACT_RATIO) -> bool:
    """True when the context ledger has crossed the threshold."""
    pct = agent.context_pct()
    if pct is None:
        return False
    return pct >= ratio * 100.0


def _select_tail(messages: list[ChatMessage], keep_turns: int) -> list[ChatMessage]:
    """Keep the last ``keep_turns`` user-started turns, cut at a user boundary.

    Cutting at a user message keeps the slice self-contained: no orphaned tool
    results referencing an assistant turn that got summarized away.
    """
    user_idxs = [i for i, m in enumerate(messages) if m.role == "user"]
    if len(user_idxs) <= keep_turns:
        return list(messages)
    cut = user_idxs[-keep_turns]
    return messages[cut:]


async def _summarize(agent: AgentInstance) -> str:
    """Run the compaction request (no tools) and return the summary text."""
    messages = agent.history.build_messages()  # [system, *history]
    messages = [*messages, ChatMessage(role="user", content=COMPACTION_PROMPT)]
    req = ChatRequest(model=agent.model, messages=messages, tools=[])
    parts: list[str] = []
    async for ev in agent.provider.stream_chat(req):
        if isinstance(ev, TextDelta):
            parts.append(ev.text)
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
    tail = _select_tail(agent.history.messages, keep_turns)
    agent.history.replace_with_summary(summary, tail)
    agent.mark_compacted()
    # Reset the ledger's running input estimate; the next real request will
    # re-measure from the smaller history.
    agent.ledger.input_tokens = 0
    agent.ledger.output_tokens = 0
    return summary
