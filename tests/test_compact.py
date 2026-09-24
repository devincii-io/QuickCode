from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from quickcode.core.agent import AgentInstance, Ledger, PermissionOutcome
from quickcode.core.compact import _select_tail, run_compaction, should_compact
from quickcode.core.events import TextDelta, TurnDone, Usage
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.providers.base import ChatMessage, ChatRequest, ProviderError


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


def _long_task(rounds: int, *, result_chars: int = 50) -> list[ChatMessage]:
    """One user request worked through ``rounds`` rounds of parallel calls."""
    msgs = [ChatMessage(role="user", content="do the big thing")]
    for i in range(rounds):
        msgs.append(ChatMessage(role="assistant", content="", tool_calls=[
            {"id": f"a{i}", "name": "read", "arguments": "{}"},
            {"id": f"b{i}", "name": "grep", "arguments": "{}"},
        ]))
        msgs.append(ChatMessage(role="tool", content="x" * result_chars, tool_call_id=f"a{i}"))
        msgs.append(ChatMessage(role="tool", content="y" * result_chars, tool_call_id=f"b{i}"))
    msgs.append(ChatMessage(role="assistant", content="all done"))
    return msgs


def _assert_pairs_intact(tail: list[ChatMessage]) -> None:
    assert not tail or tail[0].role != "tool", "the tail starts with an orphaned result"
    called = {tc["id"] for m in tail if m.role == "assistant" for tc in m.tool_calls}
    answered = {m.tool_call_id for m in tail if m.role == "tool"}
    assert called == answered


def test_one_long_turn_is_compacted_rather_than_kept_whole():
    """The commonest long session is one request worked for many rounds. With
    fewer user turns than ``keep_turns`` the whole of it was "the tail", so
    compaction summarized nothing and added the summary on top."""
    msgs = _long_task(20)
    tail = _select_tail(msgs, keep_turns=2)
    assert 0 < len(tail) < len(msgs) // 2
    assert tail[-1].content == "all done"
    _assert_pairs_intact(tail)


def test_a_tail_too_big_for_the_window_is_cut_between_rounds():
    msgs = [ChatMessage(role="user", content="earlier"), ChatMessage(role="assistant", content="ok")]
    msgs += _long_task(40, result_chars=2_000)
    tail = _select_tail(msgs, keep_turns=2, budget_chars=20_000)
    assert sum(len(m.content) for m in tail) <= 20_000
    assert tail[-1].content == "all done"
    _assert_pairs_intact(tail)


class _FailsMidway:
    async def stream_chat(self, req):
        yield TextDelta("half a summ")
        yield Usage(input_tokens=90, output_tokens=3, cost_usd=0.2)
        yield TurnDone("error", "upstream overloaded")

    async def list_models(self):
        return []


async def test_a_summary_cut_off_by_an_error_leaves_history_alone():
    agent = _agent(_FailsMidway())
    agent.history.push_user("u0")
    before = list(agent.history.messages)

    with pytest.raises(ProviderError, match="overloaded"):
        await run_compaction(agent, keep_turns=1)

    assert agent.history.messages == before


async def test_the_summarization_request_is_counted_as_spend():
    """It is the biggest request a session makes -- nearly a full window."""
    agent = _agent(StubProvider("SUMMARY"))
    q = agent.bus.subscribe(maxsize=0)
    for i in range(3):
        agent.history.push_user(f"u{i}")

    await run_compaction(agent, keep_turns=1)

    assert agent.ledger.input_tokens == 10
    assert agent.ledger.output_tokens == 5
    seen = []
    while not q.empty():
        seen.append(q.get_nowait())
    assert any(isinstance(ev, Usage) and ev.input_tokens == 10 for ev in seen)
    # Spend, not context: the rebuilt history is not the request that built it.
    assert agent.ledger.last_input_tokens == 0


def test_reasoning_is_spend_but_not_context():
    """A reasoning model's thinking is billed as output but never sent back,
    so counting it as context tripped compaction long before the window was."""
    agent = _agent(StubProvider("x"), context_length=100_000)
    agent.ledger.add(Usage(input_tokens=60_000, output_tokens=30_000, reasoning_tokens=29_000))
    assert agent.ledger.output_tokens == 30_000
    assert agent.context_pct() == 61.0
    assert should_compact(agent) is False

    replayed = Ledger.from_events([
        {"type": "usage", "input_tokens": 60_000, "output_tokens": 30_000,
         "reasoning_tokens": 29_000},
    ])
    assert replayed.last_output_tokens == agent.ledger.last_output_tokens == 1_000


def test_a_replayed_ledger_does_not_measure_context_from_before_a_compaction():
    events = [
        {"type": "usage", "input_tokens": 90_000, "output_tokens": 500},
        {"type": "compacted", "summary_chars": 1200, "manual": False},
    ]
    ledger = Ledger.from_events(events)
    assert ledger.input_tokens == 90_000
    assert ledger.last_input_tokens == 0
    assert ledger.last_output_tokens == 0


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
