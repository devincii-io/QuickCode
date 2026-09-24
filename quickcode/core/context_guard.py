"""The context guard: one long turn must not outgrow the model's window.

Compaction used to run only between turns, so a turn whose tool results grew
past the window ended in the provider's "context length exceeded" -- and
``/compact`` then overflowed as well, since its request carried the same
history. The loop calls the guard at two points (docs/ARCHITECTURE.md
§Context guard):

* ``before_request`` -- before every request of a turn. When the estimate
  (the ledger's last measured request plus chars/4 of what was appended since)
  crosses the compaction threshold, the history is compacted between rounds
  and the turn carries on.
* ``recover_from_overflow`` -- when a provider refuses a request as too long
  anyway. The largest tool results lose their middle, the history is compacted
  if that is not enough, and the loop retries the request once.

``runtime.compaction.enabled`` switches off the first and the compacting half
of the second; cutting tool results and retrying is always on, because the
alternative is a conversation no request can continue.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from quickcode.core.compact import run_compaction
from quickcode.core.context_size import (
    CHARS_PER_TOKEN,
    learn_window,
    request_chars,
    request_estimate,
    tokens,
    trim_tool_results,
)
from quickcode.core.events import AgentStatus, Compacted, ContextInjection, SystemNote
from quickcode.prompts.compact import POST_COMPACTION_REMINDER
from quickcode.prompts.system import system_reminder
from quickcode.providers.base import ProviderError

if TYPE_CHECKING:
    from quickcode.core.agent import AgentInstance


def _target(agent: AgentInstance) -> int | None:
    """Tokens a request should stay under, when the window is known."""
    window = agent.context_length
    if not window:
        return None
    return int(window * agent.limits.compaction_threshold)


async def before_request(agent: AgentInstance) -> bool:
    """Compact between rounds if the next request would cross the threshold.

    Acts only on a measured estimate: straight after a compaction nothing has
    been measured, and the request that follows is the one that measures it.
    Returns True when it tried to compact, successfully or not, so the retry
    path does not summarize the same history a second time.
    """
    target = _target(agent)
    if target is None or not agent.limits.compaction_enabled:
        return False
    estimate, measured = request_estimate(agent)
    if not measured or estimate < target:
        return False
    await compact(agent)
    return True


async def recover_from_overflow(agent: AgentInstance, error: str, *, compacted: bool) -> bool:
    """Shrink the history after ``error`` refused a request as too long.

    True when something changed and the request is worth sending once more.
    ``compacted`` says the guard already compacted ahead of this request.
    """
    learn_window(agent, error)
    target = _target(agent)
    need: int | None = None
    if target is not None:
        # The request did not fit, whatever the estimate says: it was at least
        # the whole window.
        size = max(request_estimate(agent)[0], agent.context_length or 0)
        need = (size - target) * CHARS_PER_TOKEN
    count, freed = trim_tool_results(agent.history.messages, need)
    _note_cut(agent, count)
    still_over = freed < need if need is not None else count == 0
    if still_over and agent.limits.compaction_enabled and not compacted:
        return await compact(agent) or count > 0
    return count > 0


async def compact(agent: AgentInstance) -> bool:
    """Summarize the history mid-turn, between rounds. True if it did.

    The same ``run_compaction`` the between-turn path uses, so the tail is cut
    where no call loses its result. What differs is what follows it: the turn
    is still running, so the post-compaction reminder is pushed now rather
    than on the next user message, and a failure is a note, not the end of
    the turn -- the request may still fit, and if it does not, the provider's
    refusal is what the user should see.
    """
    agent.bus.emit(AgentStatus("sending", "compacting"))
    try:
        summary = await run_compaction(agent, keep_turns=agent.limits.keep_turns)
    except ProviderError as e:
        agent.bus.emit(SystemNote(f"(compaction failed: {e})"))
        return False
    count = _fit(agent)
    # After the fit, so the history the log records is the one the next
    # request carries.
    agent.bus.emit(Compacted(len(summary), list(agent.history.messages)))
    _note_cut(agent, count)
    _remind(agent)
    return True


def _fit(agent: AgentInstance) -> int:
    """Cut tool results a freshly compacted history still cannot hold.

    The verbatim tail is capped, but never below one whole round, and a round
    whose results alone fill the window would go straight back to the
    provider over it.
    """
    target = _target(agent)
    if target is None:
        return 0
    over = tokens(request_chars(agent)) - target
    if over <= 0:
        return 0
    return trim_tool_results(agent.history.messages, over * CHARS_PER_TOKEN)[0]


def _remind(agent: AgentInstance) -> None:
    """Deliver the post-compaction reminder (and the mode, which the summary
    may have lost) as the next user turn would have -- the turn is not over."""
    reminders = []
    if agent.take_post_compaction():
        reminders.append(system_reminder(POST_COMPACTION_REMINDER))
    if mode_note := agent.take_mode_change():
        reminders.append(system_reminder(mode_note))
    for r in reminders:
        agent.bus.emit(ContextInjection(r))
    if reminders:
        agent.history.push_user("", reminders)


def _note_cut(agent: AgentInstance, count: int) -> None:
    if count:
        plural = "s" if count != 1 else ""
        agent.bus.emit(
            SystemNote(f"({count} tool result{plural} cut to fit the context window)")
        )
