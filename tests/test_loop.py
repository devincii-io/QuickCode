"""The loop's promises about one turn, driven against scripted providers.

Each test here pins a way a turn used to end with the transcript in a state
the next request could not use, or with the model told something untrue about
the calls it made: a read that ran before the write it was issued after, a
sibling result lost to one call that raised, a Stop that waited for a token
that never came, an assistant ``tool_calls`` message left with no answers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from quickcode.core.agent import AgentInstance, PermissionOutcome
from quickcode.core.events import AgentStatus, TextDelta, ToolCallEnd, TurnDone, Usage
from quickcode.core.history import History
from quickcode.core.hooks import LoopHook, PlanModeHook
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.kernel.composition import RuntimeLimits
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry


class Scripted:
    """Plays one scripted round per request, then answers in plain text."""

    def __init__(self, rounds: list[list]) -> None:
        self.rounds = list(rounds)
        self.requests: list = []

    async def stream_chat(self, req):
        self.requests.append(req)
        script = self.rounds.pop(0) if self.rounds else [TextDelta("done"), TurnDone("stop")]
        for ev in script:
            yield ev

    async def list_models(self):
        return []


async def _allow(_req):
    return PermissionOutcome(allow=True)


def _agent(tmp_path: Path, provider, *, hooks=None, limits=None, mode=Mode.auto_edit):
    ctx = ToolCtx(cwd=tmp_path, read_registry=ReadRegistry(), extra={})
    return AgentInstance(
        name="main",
        provider=provider,
        registry=default_registry(include_agent=False),
        history=History("SYS"),
        ctx=ctx,
        permissions=PermissionEngine(mode, Rules(), tmp_path),
        model="test/model",
        permission_cb=_allow,
        hooks=hooks,
        limits=limits,
    )


def _call(cid: str, name: str, **args) -> ToolCallEnd:
    return ToolCallEnd(id=cid, name=name, arguments=json.dumps(args))


def _tool_messages(agent) -> dict[str, str]:
    return {m.tool_call_id: m.content for m in agent.history.messages if m.role == "tool"}


def _drain(q: asyncio.Queue) -> list:
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def _assert_every_call_answered(agent) -> None:
    """The wire invariant: each assistant tool call is followed by its result."""
    msgs = agent.history.messages
    for i, m in enumerate(msgs):
        if m.role != "assistant" or not m.tool_calls:
            continue
        following = []
        for n in msgs[i + 1:]:
            if n.role != "tool":
                break
            following.append(n.tool_call_id)
        assert following == [tc["id"] for tc in m.tool_calls], (
            f"assistant message {i} has calls {[tc['id'] for tc in m.tool_calls]} "
            f"but is followed by results for {following}"
        )


async def test_a_read_issued_after_a_write_sees_what_was_written(tmp_path):
    target = tmp_path / "new.txt"
    provider = Scripted([[
        _call("w", "write", file_path=str(target), content="fresh contents"),
        _call("r", "read", file_path=str(target)),
        TurnDone("tool_calls"),
    ]])
    agent = _agent(tmp_path, provider)

    await agent.run_turn("write it, then show me")

    results = _tool_messages(agent)
    assert "fresh contents" in results["r"], results["r"]
    _assert_every_call_answered(agent)


class _Boom(LoopHook):
    """Raises past ``_run_tool``'s own catch for one path -- a buggy plugin hook."""

    async def intercept(self, agent, tool, args):
        if str(args.get("file_path", "")).endswith("boom.txt"):
            raise RuntimeError("hook exploded")
        return None


async def test_one_read_only_call_raising_does_not_cost_its_siblings(tmp_path):
    good = tmp_path / "good.txt"
    good.write_text("still here\n", encoding="utf-8")
    provider = Scripted([[
        _call("a", "read", file_path=str(tmp_path / "boom.txt")),
        _call("b", "read", file_path=str(good)),
        TurnDone("tool_calls"),
    ]])
    agent = _agent(tmp_path, provider, hooks=[_Boom(), PlanModeHook()])

    await agent.run_turn("read both")

    results = _tool_messages(agent)
    assert "still here" in results["b"], results["b"]
    assert "hook exploded" in results["a"], results["a"]
    assert results["a"].startswith("[error]")


class _Stalls:
    """Says a few words, then goes quiet without closing the stream."""

    def __init__(self) -> None:
        self.talking = asyncio.Event()
        self.closed = False

    async def stream_chat(self, _req):
        try:
            yield TextDelta("thinking about it")
            yield Usage(input_tokens=50, output_tokens=3)
            self.talking.set()
            await asyncio.Event().wait()
            yield TurnDone("stop")
        finally:
            self.closed = True

    async def list_models(self):
        return []


async def test_stop_ends_a_turn_whose_stream_has_gone_quiet(tmp_path):
    provider = _Stalls()
    agent = _agent(tmp_path, provider)
    q = agent.bus.subscribe(maxsize=0)

    turn = asyncio.create_task(agent.run_turn("go"))
    await asyncio.wait_for(provider.talking.wait(), 2)
    agent.cancel()
    await asyncio.wait_for(turn, 2)

    assert provider.closed, "the provider stream was left open after the interrupt"
    states = [ev.state for ev in _drain(q) if isinstance(ev, AgentStatus)]
    assert states[-1] == "interrupted"
    # The usage the provider already reported is part of the session's spend.
    assert agent.ledger.input_tokens == 50


class _Parks(LoopHook):
    """Holds every call until the test lets go -- which it never does."""

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def intercept(self, agent, tool, args):
        self.entered.set()
        await asyncio.Event().wait()


async def test_a_cancelled_turn_leaves_every_tool_call_answered(tmp_path):
    parks = _Parks()
    provider = Scripted([[
        _call("a", "read", file_path=str(tmp_path / "a.txt")),
        _call("b", "glob", pattern="*.py"),
        TurnDone("tool_calls"),
    ]])
    agent = _agent(tmp_path, provider, hooks=[parks])

    turn = asyncio.create_task(agent.run_turn("look around"))
    await asyncio.wait_for(parks.entered.wait(), 2)
    turn.cancel()
    await asyncio.gather(turn, return_exceptions=True)

    # A subagent cancelled this way stays resumable; the next request it makes
    # carries this history, and an unanswered call is a 400 from the provider.
    _assert_every_call_answered(agent)
    assert set(_tool_messages(agent)) == {"a", "b"}


async def test_the_turn_that_spends_its_budget_still_ends_idle(tmp_path):
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    again = [_call("r", "read", file_path=str(tmp_path / "f.txt")), TurnDone("tool_calls")]
    provider = Scripted([list(again), [_call("r2", "read", file_path=str(tmp_path / "f.txt")),
                                       TurnDone("tool_calls")]])
    agent = _agent(tmp_path, provider, limits=RuntimeLimits(max_rounds=1))
    q = agent.bus.subscribe(maxsize=0)

    await agent.run_turn("loop forever")

    states = [ev.state for ev in _drain(q) if isinstance(ev, AgentStatus)]
    assert states[-1] == "idle", states
    _assert_every_call_answered(agent)


async def test_an_empty_answer_is_not_kept_as_an_empty_assistant_message(tmp_path):
    provider = Scripted([[TurnDone("stop")]])
    agent = _agent(tmp_path, provider)

    await agent.run_turn("hello?")

    assert not [m for m in agent.history.messages
                if m.role == "assistant" and not m.content and not m.tool_calls]


async def test_a_call_cut_off_by_the_output_limit_says_so(tmp_path):
    provider = Scripted([[
        ToolCallEnd(id="w", name="write", arguments='{"file_path": "big.txt", "content": "aaa'),
        TurnDone("length"),
    ]])
    agent = _agent(tmp_path, provider)

    await agent.run_turn("write a huge file")

    result = _tool_messages(agent)["w"]
    assert "output limit" in result, result


async def test_arguments_that_are_not_an_object_are_refused_cleanly(tmp_path):
    provider = Scripted([[
        ToolCallEnd(id="p", name="plan", arguments='["not", "an", "object"]'),
        TurnDone("tool_calls"),
    ]])
    agent = _agent(tmp_path, provider, mode=Mode.plan)

    await agent.run_turn("plan it")

    result = _tool_messages(agent)["p"]
    assert "JSON object" in result, result
