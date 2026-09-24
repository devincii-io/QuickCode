"""Provider reasoning blocks ride from the stream into history, and no further.

A signed thinking block has to be sent back verbatim on the next request of a
tool loop, and after a resume. It is not something to render or to log as an
event: the text already went out as ``ReasoningDelta``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from quickcode.core.agent import AgentInstance, Ledger, PermissionOutcome
from quickcode.core.events import ReasoningBlock, ReasoningDelta, TextDelta, TurnDone, Usage
from quickcode.core.history import History
from quickcode.core.loop import run_turn
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.providers.base import ChatMessage, ChatRequest
from quickcode.server.serialization import event_to_json
from quickcode.session.store import message_from_dict, message_to_dict

BLOCK = {"type": "thinking", "thinking": "plan", "signature": "sig"}


class ThinkingProvider:
    async def stream_chat(self, req: ChatRequest) -> AsyncIterator:
        yield ReasoningDelta("plan")
        yield ReasoningBlock(dict(BLOCK))
        yield TextDelta("done")
        yield Usage(input_tokens=100, output_tokens=5, cached_tokens=60, cache_write_tokens=30)
        yield TurnDone("stop")

    async def list_models(self):
        return []


class NoTools:
    tools: dict = {}

    def get(self, name):
        return None


async def _deny(_req) -> PermissionOutcome:
    return PermissionOutcome(allow=False)


async def test_a_reasoning_block_lands_in_history_and_never_on_the_bus() -> None:
    agent = AgentInstance(
        name="main", provider=ThinkingProvider(), registry=NoTools(), history=History("SYS"),
        ctx=None, permissions=PermissionEngine(Mode.ask, Rules(), Path.cwd()),
        model="m", permission_cb=_deny,
    )
    queue = agent.bus.subscribe(maxsize=0)
    await run_turn(agent, "hi")

    seen = []
    while not queue.empty():
        seen.append(queue.get_nowait())
    assert not any(isinstance(e, ReasoningBlock) for e in seen)
    assert agent.history.messages[-1].reasoning_blocks == [BLOCK]
    assert (agent.ledger.cached_tokens, agent.ledger.cache_write_tokens) == (60, 30)


def test_reasoning_blocks_survive_a_resume_and_leave_other_messages_unchanged() -> None:
    plain = ChatMessage(role="assistant", content="a")
    assert "reasoning_blocks" not in message_to_dict(plain)

    signed = ChatMessage(role="assistant", content="a", reasoning_blocks=[dict(BLOCK)])
    assert message_from_dict(message_to_dict(signed)).reasoning_blocks == [BLOCK]


def test_cache_writes_are_logged_and_counted_again_on_resume() -> None:
    wire = event_to_json(Usage(input_tokens=10, cache_write_tokens=4))
    assert wire["cache_write_tokens"] == 4
    assert Ledger.from_events([wire]).cache_write_tokens == 4


def _signed_history() -> History:
    history = History("SYS")
    history.push_user("hi")
    history.messages.append(
        ChatMessage(role="assistant", content="a", reasoning_blocks=[dict(BLOCK)])
    )
    return history


def test_a_rewritten_history_lets_go_of_its_signed_reasoning() -> None:
    """A signature binds a block to the history before it. After a compaction
    or a new system prompt the API refuses the block, so it is dropped at that
    boundary instead of being refused on every request that follows."""
    compacted = _signed_history()
    compacted.replace_with_summary("summary", list(compacted.messages))
    assert all(not m.reasoning_blocks for m in compacted.messages)

    switched = _signed_history()
    switched.set_system_prompt("SYS, now for another model")
    assert all(not m.reasoning_blocks for m in switched.messages)


def test_an_unchanged_system_prompt_keeps_the_reasoning() -> None:
    history = _signed_history()
    history.set_system_prompt("SYS")
    assert history.messages[-1].reasoning_blocks == [BLOCK]
