from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from quickcode.core.agent import AgentInstance, PermissionOutcome
from quickcode.core.compact import _select_tail, run_compaction, should_compact
from quickcode.core.events import TextDelta, TurnDone, Usage
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.providers.base import ChatMessage, ChatRequest


class StubProvider:
    """Returns a canned text response; records the last request it saw."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.last_request: ChatRequest | None = None

    async def stream_chat(self, req: ChatRequest) -> AsyncIterator:
        self.last_request = req
        yield TextDelta(self.text)
        yield Usage(input_tokens=10, output_tokens=5)
        yield TurnDone("stop")

    async def list_models(self):
        return []


class StubRegistry:
    def schemas(self):
        return []

    def get(self, name):
        return None

    tools: dict = {}


async def _deny(_req) -> PermissionOutcome:
    return PermissionOutcome(allow=False)


def _agent(provider, context_length=100) -> AgentInstance:
    history = History("SYS")
    return AgentInstance(
        name="main",
        provider=provider,
        registry=StubRegistry(),
        history=history,
        ctx=None,
        permissions=PermissionEngine(Mode.ask, Rules(), Path.cwd()),
        model="test/model",
        permission_cb=_deny,
        context_length=context_length,
    )


def test_select_tail_cuts_at_user_boundary():
    msgs = [
        ChatMessage(role="user", content="u1"),
        ChatMessage(role="assistant", content="a1"),
        ChatMessage(role="user", content="u2"),
        ChatMessage(role="assistant", content="a2"),
        ChatMessage(role="user", content="u3"),
        ChatMessage(role="assistant", content="a3"),
    ]
    tail = _select_tail(msgs, keep_turns=2)
    assert tail[0].role == "user" and tail[0].content == "u2"
    assert len(tail) == 4


def test_should_compact_threshold():
    agent = _agent(StubProvider("x"), context_length=100)
    agent.ledger.last_input_tokens = 85
    assert should_compact(agent) is True
    agent.ledger.last_input_tokens = 10
    assert should_compact(agent) is False


async def test_run_compaction_rebuilds_history_and_reminds():
    provider = StubProvider("SUMMARY: did the thing")
    agent = _agent(provider)
    for i in range(4):
        agent.history.push_user(f"u{i}")
        from quickcode.core.events import AssistantMessage

        agent.history.push_assistant(AssistantMessage(text=f"a{i}"))

    summary = await run_compaction(agent, keep_turns=1)
    assert "SUMMARY" in summary
    # first message is now the summary seed
    assert agent.history.messages[0].role == "user"
    assert "compaction-summary" in agent.history.messages[0].content
    # the compaction request carried the COMPACTION_PROMPT as the last user msg
    assert "handoff summary" in provider.last_request.messages[-1].content

    # run_compaction armed the post-compaction reminder; the next turn injects it
    await agent.run_turn("continue")
    joined = " ".join(m.content for m in agent.history.messages if m.role == "user")
    assert "summarized" in joined
    # and the flag is now consumed
    assert agent.take_post_compaction() is False


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
