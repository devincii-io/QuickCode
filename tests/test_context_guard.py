"""The context guard: one long turn must not wedge the conversation.

Compaction ran only between turns, so a single turn whose tool results grew
past the model's window ended in the provider's "context length exceeded" --
and ``/compact`` overflowed too, because its request carried the same history.
These pin the three halves of the fix: a compaction between rounds when the
next request would cross the threshold, one shrink-and-retry when a provider
refuses a request for length anyway, and a summary request that fits.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from quickcode.core.agent import AgentInstance, Ledger, PermissionOutcome
from quickcode.core.compact import run_compaction, should_compact
from quickcode.core.context_size import cut, trim_tool_results
from quickcode.core.events import (
    AgentStatus,
    AssembledToolCall,
    AssistantMessage,
    Compacted,
    SystemNote,
    TextDelta,
    ToolCallEnd,
    TurnDone,
    Usage,
)
from quickcode.core.history import History
from quickcode.core.permissions import READ_LIKE, Mode, PermissionEngine, Rules
from quickcode.kernel.composition import RuntimeLimits
from quickcode.prompts.compact import COMPACTION_PROMPT
from quickcode.providers.base import ChatMessage, ProviderError
from quickcode.providers.overflow import is_context_overflow, overflow_limit
from quickcode.tools.base import ReadRegistry, Tool, ToolCtx, ToolResult
from quickcode.tools.registry import ToolRegistry
from tests.test_server import FakeProvider, make_manager

OPENAI_OVERFLOW = (
    "Error code: 400 - {'error': {'message': \"This model's maximum context length is "
    "8192 tokens. However, your messages resulted in 14230 tokens. Please reduce the "
    "length of the messages.\", 'type': 'invalid_request_error', 'param': 'messages', "
    "'code': 'context_length_exceeded'}}"
)


def anthropic_overflow(limit: int) -> str:
    return (
        f"Anthropic API 400 invalid_request_error: prompt is too long: {limit + 1500} "
        f"tokens > {limit} maximum (request req_011CT)"
    )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class DumpArgs(BaseModel):
    chars: int = Field(description="How many characters to return.")


class Dump(Tool):
    """A read-only tool whose output is exactly as long as it is asked to be."""

    name = "dump"
    description = "Returns a block of text of the requested size."
    is_read_only = True
    permission = READ_LIKE
    Input = DumpArgs

    async def run(self, input, ctx):  # noqa: A002
        return ToolResult(content="HEAD" + "x" * max(0, input.chars - 8) + "TAIL")


class Scripted:
    """One scripted round per request; an exception in a script is raised."""

    def __init__(self, rounds: list[list]) -> None:
        self.rounds = list(rounds)
        self.requests: list = []

    async def stream_chat(self, req):
        self.requests.append(req)
        script = self.rounds.pop(0) if self.rounds else [TextDelta("done"), TurnDone("stop")]
        for ev in script:
            if isinstance(ev, Exception):
                raise ev
            yield ev

    async def list_models(self):
        return []


async def _allow(_req) -> PermissionOutcome:
    return PermissionOutcome(allow=True)


def _agent(tmp_path: Path, provider, *, window: int | None = 10_000,
           limits: RuntimeLimits | None = None) -> AgentInstance:
    return AgentInstance(
        name="main",
        provider=provider,
        registry=ToolRegistry([Dump()]),
        history=History("SYS"),
        ctx=ToolCtx(cwd=tmp_path, read_registry=ReadRegistry(), extra={}),
        permissions=PermissionEngine(Mode.ask, Rules(), tmp_path),
        model="test/model",
        permission_cb=_allow,
        context_length=window,
        limits=limits,
    )


def _dump(cid: str, chars: int) -> ToolCallEnd:
    return ToolCallEnd(id=cid, name="dump", arguments=json.dumps({"chars": chars}))


def _is_summary(req) -> bool:
    return req.messages[-1].content == COMPACTION_PROMPT


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _tool_text(req, cid: str) -> str:
    return next(m.content for m in req.messages if m.role == "tool" and m.tool_call_id == cid)


def _assert_every_call_answered(messages) -> None:
    for i, m in enumerate(messages):
        if m.role == "assistant" and m.tool_calls:
            ids = [tc["id"] for tc in m.tool_calls]
            answers = [x.tool_call_id for x in messages[i + 1:i + 1 + len(ids)]]
            assert answers == ids, f"calls {ids} answered by {answers}"


SUMMARY = [TextDelta("SUMMARY: dumped a big block"), Usage(input_tokens=5_000, output_tokens=40),
           TurnDone("stop")]


# --------------------------------------------------------------------------
# recognising a refusal for length
# --------------------------------------------------------------------------


@pytest.mark.parametrize("message, limit", [
    (OPENAI_OVERFLOW, 8192),
    ("This endpoint's maximum context length is 200000 tokens. However, you requested about "
     "213520 tokens (197136 of text input, 16384 in the output). Please reduce the length of "
     "either one, or use the \"middle-out\" transform to compress your prompt automatically.",
     200_000),
    (anthropic_overflow(200_000), 200_000),
    ("Anthropic API 400 invalid_request_error: input length and `max_tokens` exceed context "
     "limit: 188240 + 21333 > 200000, decrease input length or `max_tokens` and try again",
     200_000),
    ("Your input exceeds the context window of this model. Please adjust your input and try "
     "again.", None),
    ("request (9000 tokens) exceeds the available context size (8192 tokens), try increasing "
     "it", 8192),
    ("The input token count (1200000) exceeds the maximum number of tokens allowed (1048576).",
     1_048_576),
    ("Prompt contains 40000 tokens and 0 draft tokens, too large for model with 32768 maximum "
     "context length", 32_768),
    ("Anthropic API 413 request_too_large: Request exceeds the maximum allowed number of bytes.",
     None),
])
def test_refusals_for_length_are_recognised_and_name_their_window(message, limit):
    assert is_context_overflow(message)
    assert overflow_limit(message) == limit


@pytest.mark.parametrize("message", [
    "Rate limit reached for gpt-4o in organization org-x on tokens per min (TPM): "
    "Limit 30000, Used 25000, Requested 9000.",
    "Error code: 401 - Incorrect API key provided",
    "max_tokens is too large: 100000. This model supports at most 16384 completion tokens.",
    "Anthropic API 529 overloaded_error: Overloaded",
])
def test_other_refusals_are_not_mistaken_for_length(message):
    assert not is_context_overflow(message)


def test_a_cut_keeps_head_and_tail_and_says_why():
    text = "HEAD" + "x" * 50_000 + "TAIL"
    out = cut(text, 2_000)
    assert out.startswith("HEAD") and out.endswith("TAIL")
    assert 'total="50008"' in out and "context window" in out
    assert len(out) < 2_400


def test_one_giant_result_is_all_that_is_cut_when_it_alone_is_the_problem():
    msgs = [ChatMessage(role="tool", content="a" * 60_000, tool_call_id="big")]
    msgs += [ChatMessage(role="tool", content="b" * 3_000, tool_call_id=f"s{i}") for i in range(5)]
    count, freed = trim_tool_results(msgs, 30_000)
    assert count == 1 and freed >= 30_000
    assert all(len(m.content) == 3_000 for m in msgs[1:])


def test_many_middling_results_each_give_up_a_little():
    msgs = [ChatMessage(role="tool", content="r" * 4_000, tool_call_id=f"c{i}")
            for i in range(20)]
    count, freed = trim_tool_results(msgs, 30_000)
    assert freed >= 30_000
    # Shared out: most of them lose some, none of them loses nearly all.
    assert count >= 15
    assert min(len(m.content) for m in msgs) > 2_000


# --------------------------------------------------------------------------
# 1. compacting between rounds
# --------------------------------------------------------------------------


async def test_a_turn_whose_tool_results_overflow_compacts_mid_turn_and_finishes(tmp_path):
    provider = Scripted([
        [_dump("c1", 30_000), Usage(input_tokens=2_000, output_tokens=50), TurnDone("tool_calls")],
        SUMMARY,
        [TextDelta("finished"), Usage(input_tokens=1_500, output_tokens=10), TurnDone("stop")],
    ])
    agent = _agent(tmp_path, provider)
    q = agent.bus.subscribe(maxsize=0)

    assert await agent.run_turn("dump it") == "finished"

    # The request after the 30k-char result would have been ~9.5k of a 10k
    # window: it was preceded by a summary instead of sent over the threshold.
    assert [_is_summary(r) for r in provider.requests] == [False, True, False]
    msgs = agent.history.messages
    assert msgs[0].content.startswith("<compaction-summary>SUMMARY")
    assert msgs[-1].role == "assistant" and msgs[-1].content == "finished"
    _assert_every_call_answered(msgs)
    # The reminder the next user message would have carried is delivered now:
    # the turn is still running, and the model needs it before its next move.
    last = provider.requests[2].messages
    assert "summarized" in last[-1].content and last[-1].role == "user"
    assert agent.take_post_compaction() is False
    events = _drain(q)
    assert sum(isinstance(e, Compacted) for e in events) == 1
    assert not any(isinstance(e, TurnDone) and e.error for e in events)
    # Nothing left for the between-turn check to do.
    assert should_compact(agent) is False


async def test_the_summary_request_fits_even_when_the_history_does_not(tmp_path):
    provider = Scripted([
        [_dump("c1", 30_000), Usage(input_tokens=2_000, output_tokens=50), TurnDone("tool_calls")],
        SUMMARY,
    ])
    agent = _agent(tmp_path, provider)

    await agent.run_turn("dump it")

    summary_req = provider.requests[1]
    assert "<truncated" in _tool_text(summary_req, "c1")
    # Only the request was cut: the verbatim tail kept the result whole.
    kept = next(m for m in agent.history.messages if m.role == "tool")
    assert len(kept.content) == 30_000


async def test_the_summary_request_leaves_out_the_oldest_rounds_when_cutting_is_not_enough(
    tmp_path,
):
    """Long user messages are not tool results; nothing can be cut from them."""
    provider = Scripted([SUMMARY])
    agent = _agent(tmp_path, provider)
    for i in range(4):
        agent.history.push_user(f"U{i} " + "p" * 12_000)
        agent.history.push_assistant(AssistantMessage(text=f"answer {i}"))

    await run_compaction(agent, keep_turns=1)

    sent = [m.content[:3] for m in provider.requests[0].messages if m.role == "user"]
    assert "U0 " not in sent and "U3 " in sent
    assert sum(len(m.content) for m in provider.requests[0].messages) < 10_000 * 4


async def test_a_summary_refused_for_length_is_fitted_again_once(tmp_path):
    provider = Scripted([[ProviderError(anthropic_overflow(6_000))], SUMMARY])
    agent = _agent(tmp_path, provider, window=None)
    call = AssembledToolCall(id="c1", name="dump", arguments="{}")
    agent.history.push_user("go")
    agent.history.push_assistant(AssistantMessage(tool_calls=[call]))
    agent.history.push_tool_results([(call, "z" * 40_000, False)])

    summary = await run_compaction(agent, keep_turns=1)

    assert summary.startswith("SUMMARY")
    assert agent.context_length == 6_000
    assert "<truncated" in _tool_text(provider.requests[1], "c1")


async def test_with_compaction_off_the_turn_is_not_compacted(tmp_path):
    provider = Scripted([
        [_dump("c1", 30_000), Usage(input_tokens=2_000, output_tokens=50), TurnDone("tool_calls")],
        [TextDelta("done"), TurnDone("stop")],
    ])
    agent = _agent(tmp_path, provider, limits=RuntimeLimits(compaction_enabled=False))

    assert await agent.run_turn("dump it") == "done"
    assert not any(_is_summary(r) for r in provider.requests)


# --------------------------------------------------------------------------
# 2. a refusal for length: shrink, retry once
# --------------------------------------------------------------------------


async def test_a_refusal_for_length_cuts_the_largest_result_and_retries_once(tmp_path):
    provider = Scripted([
        [_dump("big", 50_000), _dump("small", 3_000), TurnDone("tool_calls")],
        [ProviderError(OPENAI_OVERFLOW)],
        [TextDelta("recovered"), TurnDone("stop")],
    ])
    agent = _agent(tmp_path, provider, window=None)
    q = agent.bus.subscribe(maxsize=0)

    assert await agent.run_turn("go") == "recovered"

    assert len(provider.requests) == 3
    assert not any(_is_summary(r) for r in provider.requests)
    retry = provider.requests[2]
    big = _tool_text(retry, "big")
    assert big.startswith("HEAD") and big.endswith("TAIL") and "<truncated" in big
    assert _tool_text(retry, "small") == _tool_text(provider.requests[1], "small")
    # The provider named the window; the agent that had none now knows it.
    assert agent.context_length == 8192
    events = _drain(q)
    # A refusal the retry recovered from is not an error anyone is shown --
    # `-p` reads a TurnDone error as the run having failed.
    assert not any(isinstance(e, TurnDone) and e.error for e in events)
    assert not any(isinstance(e, AgentStatus) and e.state == "error" for e in events)
    assert any(isinstance(e, SystemNote) and "cut to fit" in e.text for e in events)
    # The history carries the cut, or the next request would be refused again.
    assert "<truncated" in next(
        m.content for m in agent.history.messages if m.tool_call_id == "big"
    )


async def test_a_second_refusal_surfaces_the_error_as_before(tmp_path):
    provider = Scripted([
        [_dump("big", 50_000), TurnDone("tool_calls")],
        [ProviderError(OPENAI_OVERFLOW)],
        [ProviderError(OPENAI_OVERFLOW)],
    ])
    agent = _agent(tmp_path, provider, window=None)
    q = agent.bus.subscribe(maxsize=0)

    await agent.run_turn("go")

    assert len(provider.requests) == 3, "retried more than once"
    events = _drain(q)
    errors = [e for e in events if isinstance(e, TurnDone) and e.error]
    assert len(errors) == 1 and "maximum context length" in errors[0].error
    assert any(isinstance(e, AgentStatus) and e.state == "error" for e in events)
    _assert_every_call_answered(agent.history.messages)


async def test_a_refusal_in_the_stream_is_held_back_and_recovered_from_too(tmp_path):
    """A provider may report the refusal as a TurnDone rather than raise it."""
    provider = Scripted([
        [_dump("big", 50_000), TurnDone("tool_calls")],
        [TurnDone("error", OPENAI_OVERFLOW)],
        [TextDelta("recovered"), TurnDone("stop")],
    ])
    agent = _agent(tmp_path, provider, window=None)
    q = agent.bus.subscribe(maxsize=0)

    assert await agent.run_turn("go") == "recovered"
    assert not any(isinstance(e, TurnDone) and e.error for e in _drain(q))


async def test_a_refusal_that_cutting_cannot_fix_is_compacted_then_retried(tmp_path):
    """Nothing to cut -- the window is full of conversation, not tool output."""
    provider = Scripted([
        [_dump("c1", 100), TurnDone("tool_calls")],
        [ProviderError(anthropic_overflow(10_000))],
        SUMMARY,
        [TextDelta("done"), TurnDone("stop")],
    ])
    agent = _agent(tmp_path, provider)
    for i in range(3):
        agent.history.push_user(f"OLD-{i} " + "p" * 12_000)
        agent.history.push_assistant(AssistantMessage(text=f"answer {i}"))

    assert await agent.run_turn("go") == "done"

    assert [_is_summary(r) for r in provider.requests] == [False, False, True, False]
    assert not any("OLD-0" in m.content for m in provider.requests[3].messages)
    _assert_every_call_answered(agent.history.messages)


async def test_with_compaction_off_only_the_cut_and_retry_applies(tmp_path):
    provider = Scripted([
        [_dump("c1", 100), TurnDone("tool_calls")],
        [ProviderError(anthropic_overflow(10_000))],
    ])
    agent = _agent(tmp_path, provider, limits=RuntimeLimits(compaction_enabled=False))
    for i in range(3):
        agent.history.push_user(f"OLD-{i} " + "p" * 12_000)
        agent.history.push_assistant(AssistantMessage(text=f"answer {i}"))

    await agent.run_turn("go")

    # Nothing to cut, and no summary allowed: the refusal is the answer.
    assert len(provider.requests) == 2
    assert not any(_is_summary(r) for r in provider.requests)


async def test_a_compacted_request_that_is_still_refused_is_not_compacted_twice(tmp_path):
    provider = Scripted([
        [_dump("c1", 30_000), Usage(input_tokens=2_000, output_tokens=50), TurnDone("tool_calls")],
        SUMMARY,
        [ProviderError(anthropic_overflow(10_000))],
        [TextDelta("done"), TurnDone("stop")],
    ])
    agent = _agent(tmp_path, provider)
    q = agent.bus.subscribe(maxsize=0)

    assert await agent.run_turn("go") == "done"

    assert [_is_summary(r) for r in provider.requests] == [False, True, False, False]
    assert sum(isinstance(e, Compacted) for e in _drain(q)) == 1
    assert "<truncated" in _tool_text(provider.requests[3], "c1")


# --------------------------------------------------------------------------
# the session log, the web worker and subagents
# --------------------------------------------------------------------------


async def _settle(conv, *, timeout: float = 10.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
        if not conv.agent.busy and conv._inbox.empty() and not conv.input_queue:
            return
    raise AssertionError("the conversation never went idle")


def _big_file(tmp_path: Path) -> Path:
    path = tmp_path / "big.txt"
    path.write_text("".join(f"line {i:04d} " + "y" * 50 + "\n" for i in range(1_000)))
    return path


async def test_a_web_turn_compacted_mid_turn_is_logged_once_and_resumes(tmp_path):
    big = _big_file(tmp_path)
    provider = FakeProvider([
        [ToolCallEnd("c1", "read", json.dumps({"file_path": str(big)})),
         Usage(input_tokens=1_500, output_tokens=20), TurnDone("tool_calls")],
        [TextDelta("SUMMARY of the read"), Usage(input_tokens=9_000, output_tokens=30),
         TurnDone("stop")],
        [TextDelta("all done"), Usage(input_tokens=3_000, output_tokens=10), TurnDone("stop")],
    ])
    manager = make_manager(tmp_path, provider)
    conv = manager.open()
    try:
        conv.agent.context_length = 12_000
        conv.submit("read the big file")
        await _settle(conv)

        events = conv.store.load_events()
        kinds = [e["type"] for e in events]
        compacted = [e for e in events if e["type"] == "compacted"]
        # Once, mid-turn -- and the between-turn check did not run it again.
        assert len(compacted) == 1 and compacted[0]["mid_turn"] is True
        assert compacted[0]["manual"] is False
        assert kinds.index("tool_result") < kinds.index("compacted")
        assert kinds.index("compacted") < len(kinds) - 1 - kinds[::-1].index("assistant_message")
        assert any(e["type"] == "system_note" and "earlier rounds" in e["text"] for e in events)

        # What a resume loads: the rebuilt history plus what followed, once.
        messages = conv.store.load_messages()
        assert messages[0].content.startswith("<compaction-summary>SUMMARY of the read")
        assert sum("compaction-summary" in m.content for m in messages) == 1
        assert messages[-1].role == "assistant" and messages[-1].content == "all done"
        assert len(messages) == len(conv.agent.history.messages)
        _assert_every_call_answered(messages)

        # And a replayed ledger measures the context the way the live one does.
        replayed = Ledger.from_events(events)
        assert replayed.last_input_tokens == conv.agent.ledger.last_input_tokens == 3_000
    finally:
        await manager.close()


async def test_a_headless_turn_is_compacted_mid_turn_and_not_again_after_it(tmp_path):
    from quickcode.session.recorder import TranscriptRecorder
    from quickcode.session.store import SessionStore

    provider = Scripted([
        [_dump("c1", 30_000), Usage(input_tokens=2_000, output_tokens=50), TurnDone("tool_calls")],
        SUMMARY,
        [TextDelta("finished"), Usage(input_tokens=1_500, output_tokens=10), TurnDone("stop")],
    ])
    agent = _agent(tmp_path, provider)
    store = SessionStore(tmp_path)
    rec = TranscriptRecorder(store)

    assert await rec.record_turn(agent, "dump it") == "finished"

    assert [e["type"] for e in store.load_events()].count("compacted") == 1
    assert len(provider.requests) == 3
    assert rec.persisted == len(agent.history.messages)
    assert [m.content for m in store.load_messages()] == [
        m.content for m in agent.history.messages
    ]


async def test_a_subagent_runs_the_guard_on_the_window_of_its_own_model(tmp_path):
    from quickcode.config import Environment, Profile
    from quickcode.subagents.runner import SubagentDeps, spawn_subagent

    big = _big_file(tmp_path)
    provider = FakeProvider([
        [ToolCallEnd("c1", "read", json.dumps({"file_path": str(big)})),
         Usage(input_tokens=1_500, output_tokens=20), TurnDone("tool_calls")],
        [TextDelta("SUMMARY of the read"), Usage(input_tokens=9_000, output_tokens=30),
         TurnDone("stop")],
        [TextDelta("the report"), Usage(input_tokens=3_000, output_tokens=10), TurnDone("stop")],
    ])
    asked: list[str] = []

    def window(model: str) -> int:
        asked.append(model)
        return 12_000

    deps = SubagentDeps(
        provider=provider, profile=Profile(), env=Environment.detect(tmp_path),
        mode_getter=lambda: Mode.ask, cwd=tmp_path, context_window=window,
    )

    agent_id, report, _status = await spawn_subagent(deps, agent_type="explore", prompt="read it")

    child = deps.roster[agent_id]
    assert asked == [child.model] and child.context_length == 12_000
    assert "the report" in report
    assert child.history.messages[0].content.startswith("<compaction-summary>")
    assert sum(1 for r in provider.requests if _is_summary(r)) == 1


async def test_a_headless_run_hands_its_catalog_windows_to_the_subagents_it_spawns(tmp_path):
    from quickcode import cli
    from quickcode.config import Environment, Profile
    from quickcode.subagents.runner import SubagentDeps

    deps = SubagentDeps(
        provider=FakeProvider([]), profile=Profile(), env=Environment.detect(tmp_path),
        mode_getter=lambda: Mode.ask, cwd=tmp_path,
    )
    agent = _agent(tmp_path, FakeProvider([]), window=None)
    agent.ctx.extra["subagent"] = deps

    await cli._warm_context_length(agent)

    assert agent.context_length == 100_000
    assert deps.window_for("test/model") == 100_000
    assert deps.window_for("unknown/model") is None


def test_a_child_on_the_spawners_model_inherits_its_window(tmp_path):
    from quickcode.config import Environment, Profile
    from quickcode.subagents.runner import SubagentDeps

    owner = _agent(tmp_path, Scripted([]), window=64_000)
    deps = SubagentDeps(
        provider=owner.provider, profile=Profile(), env=Environment.detect(tmp_path),
        mode_getter=lambda: Mode.ask, cwd=tmp_path, owner=owner,
    )
    assert deps.window_for("test/model") == 64_000
    assert deps.window_for("another/model") is None
    deps.context_window = {"another/model": 32_000}.get
    assert deps.window_for("another/model") == 32_000
