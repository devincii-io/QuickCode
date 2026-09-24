"""send_message: resuming a finished subagent with its context intact instead
of respawning a fresh one."""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from quickcode.config import Environment, Profile
from quickcode.core.events import TextDelta, ToolCallEnd, TurnDone
from quickcode.core.permissions import READ_LIKE, Mode
from quickcode.subagents.definitions import AgentDef, builtin_defs
from quickcode.subagents.jobs import CANCELLED, DONE
from quickcode.subagents.runner import (
    SubagentDeps,
    resume_subagent,
    spawn_subagent,
    spawn_subagent_background,
)
from quickcode.tools.agent import AgentTool
from quickcode.tools.base import Tool, ToolResult
from quickcode.tools.registry import build_registry, default_registry
from quickcode.tools.send_message import SendMessageTool
from tests.test_server import FakeProvider


class ScriptedProvider:
    """Emits a fixed final message for whichever turn is currently running.

    ``texts`` is consumed one item per turn so a test can script a spawn
    followed by one or more resumes with distinct replies.
    """

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls = 0

    async def stream_chat(self, req):
        text = self.texts[min(self.calls, len(self.texts) - 1)]
        self.calls += 1
        yield TextDelta(text)
        yield TurnDone("stop")

    async def list_models(self):
        return []


def _deps(provider, mode=Mode.ask, depth=0, cwd=None):
    cwd = cwd or Path.cwd()
    return SubagentDeps(
        provider=provider,
        profile=Profile(),
        env=Environment.detect(cwd),
        mode_getter=lambda: mode,
        cwd=cwd,
        depth=depth,
    )


async def test_spawn_registers_child_in_roster():
    deps = _deps(ScriptedProvider("first"))
    agent_id, _, _ = await spawn_subagent(deps, agent_type="explore", prompt="find it")
    assert agent_id in deps.roster
    assert deps.roster[agent_id].name == agent_id


async def test_resume_returns_sanitized_report_and_keeps_history():
    deps = _deps(ScriptedProvider("first reply", "second reply"))
    agent_id, first_report, _ = await spawn_subagent(
        deps, agent_type="general", prompt="do the first thing"
    )
    assert "first reply" in first_report

    resumed_id, second_report, _ = await resume_subagent(
        deps, agent_id=agent_id, message="now do the second thing"
    )
    assert resumed_id == agent_id
    assert "second reply" in second_report
    assert second_report.startswith("[quickcode: sanitized")

    child = deps.roster[agent_id]
    # The full turn history survives across the resume: both the original
    # prompt/reply and the follow-up prompt/reply are present.
    joined = "\n".join(m.content or "" for m in child.history.messages)
    assert "do the first thing" in joined
    assert "first reply" in joined
    assert "now do the second thing" in joined
    assert "second reply" in joined


async def test_resume_unknown_id_lists_known_ids():
    deps = _deps(ScriptedProvider("first"))
    a1, _, _ = await spawn_subagent(deps, agent_type="explore", prompt="p1")
    a2, _, _ = await spawn_subagent(deps, agent_type="general", prompt="p2")

    with pytest.raises(ValueError, match="unknown agent_id 'nope'") as exc_info:
        await resume_subagent(deps, agent_id="nope", message="hi")
    msg = str(exc_info.value)
    assert a1 in msg
    assert a2 in msg


async def test_resume_empty_roster_reports_none():
    deps = _deps(ScriptedProvider("x"))
    with pytest.raises(ValueError, match=r"Known: \(none\)"):
        await resume_subagent(deps, agent_id="nope", message="hi")


async def test_resume_while_busy_raises():
    deps = _deps(ScriptedProvider("first"))
    agent_id, _, _ = await spawn_subagent(deps, agent_type="general", prompt="p")
    child = deps.roster[agent_id]
    child.busy = True  # simulate an in-flight turn
    with pytest.raises(ValueError, match=f"agent '{agent_id}' is still running"):
        await resume_subagent(deps, agent_id=agent_id, message="hi")


async def test_send_message_tool_round_trips_through_deps():
    from quickcode.tools.base import ReadRegistry, ToolCtx

    deps = _deps(ScriptedProvider("first reply", "second reply"))
    agent_id, _, _ = await spawn_subagent(deps, agent_type="general", prompt="p1")

    ctx = ToolCtx(cwd=Path.cwd(), read_registry=ReadRegistry(), extra={"subagent": deps})
    tool = SendMessageTool()
    result = await tool.run(SendMessageTool.Input(agent_id=agent_id, message="p2"), ctx)
    assert not result.is_error
    assert f'<subagent id="{agent_id}" status="done">' in result.content
    assert "second reply" in result.content


async def test_send_message_tool_unavailable_without_deps():
    from quickcode.tools.base import ReadRegistry, ToolCtx

    ctx = ToolCtx(cwd=Path.cwd(), read_registry=ReadRegistry(), extra={})
    tool = SendMessageTool()
    result = await tool.run(SendMessageTool.Input(agent_id="x", message="hi"), ctx)
    assert result.is_error
    assert "unavailable" in result.content


def test_send_message_is_read_only():
    assert SendMessageTool().is_read_only is True


def test_default_registry_includes_send_message_alongside_agent():
    with_agent = default_registry(include_agent=True)
    assert "agent" in with_agent.tools
    assert "send_message" in with_agent.tools

    without_agent = default_registry(include_agent=False)
    assert "agent" not in without_agent.tools
    assert "send_message" not in without_agent.tools


def test_build_registry_includes_send_message_alongside_agent():
    with_agent = build_registry(["read"], include_agent=True)
    assert "agent" in with_agent.tools
    assert "send_message" in with_agent.tools

    without_agent = build_registry(["read"], include_agent=False)
    assert "agent" not in without_agent.tools
    assert "send_message" not in without_agent.tools


def test_agent_tool_description_mentions_send_message():
    assert "send_message" in AgentTool().description


# --------------------------------------------------------------------------
# resuming a child that was cut off, and one that has not started yet
# --------------------------------------------------------------------------


def _unanswered_calls(messages) -> list[str]:
    """Tool-call ids an assistant message made that no tool message answers.

    OpenAI-compatible providers refuse a request containing one, so a history
    with any is a child every later ``send_message`` fails on.
    """
    answered = {m.tool_call_id for m in messages if m.role == "tool"}
    return [
        call["id"]
        for m in messages if m.role == "assistant"
        for call in (m.tool_calls or [])
        if call["id"] not in answered
    ]


class _SlowInput(BaseModel):
    what: str = ""


class _SlowTool(Tool[_SlowInput]):
    """A read-only tool that blocks until cancelled."""

    name = "slow"
    description = "blocks"
    is_read_only = True
    permission = READ_LIKE
    Input = _SlowInput

    def __init__(self) -> None:
        self.entered = asyncio.Event()

    async def run(self, input, ctx):  # noqa: A002
        self.entered.set()
        await asyncio.Event().wait()
        return ToolResult("never")


async def test_a_child_cut_off_mid_tool_call_can_still_be_resumed(tmp_path):
    """Cancellation reaches a child as ``CancelledError`` -- the parent's
    interrupt cancels the gather it runs in, a job's cancel cancels its task --
    and that unwinds the loop past the point where tool results are pushed.
    The child's history kept an assistant message whose calls nothing
    answered, and every provider request it made after that was refused."""
    provider = FakeProvider([
        [ToolCallEnd(id="c1", name="slow", arguments="{}"), TurnDone("tool_calls")],
        [TextDelta("picked it back up"), TurnDone("stop")],
    ])
    slow = _SlowTool()
    owned: list[asyncio.Task] = []
    deps = _deps(provider, cwd=tmp_path)
    deps.adopt_task = owned.append
    deps.pool = [slow, *default_registry().tools.values()]
    deps.defs = {**builtin_defs(), "waiter": AgentDef("waiter", "w", tools=["slow"])}

    job = spawn_subagent_background(deps, agent_type="waiter", prompt="wait")
    await slow.entered.wait()
    deps.cancel_jobs()
    await asyncio.gather(*owned, return_exceptions=True)
    assert job.status == CANCELLED

    _id, report, status = await resume_subagent(
        deps, agent_id=job.agent_id, message="carry on"
    )
    assert status == DONE and "picked it back up" in report
    sent = provider.requests[-1].messages
    assert _unanswered_calls(sent) == []
    assert [m.content for m in sent if m.role == "tool"] == ["[error] [interrupted]"]


async def test_a_job_that_has_not_started_yet_cannot_be_resumed_under_it(tmp_path):
    """``busy`` flips when the child's turn starts, which for a detached job is
    the task's first step -- after the spawning round's other calls have run.
    A ``send_message`` in the same round saw an idle child and started a
    second turn on it, so two turns shared one history."""
    deps = _deps(ScriptedProvider("first"), cwd=tmp_path)
    owned: list[asyncio.Task] = []
    deps.adopt_task = owned.append
    job = spawn_subagent_background(deps, agent_type="explore", prompt="p")

    with pytest.raises(ValueError, match="still running"):
        await resume_subagent(deps, agent_id=job.agent_id, message="hi")

    await asyncio.gather(*owned)
    _id, report, _status = await resume_subagent(deps, agent_id=job.agent_id, message="hi")
    assert "first" in report


# --------------------------------------------------------------------------
# who may drive whom
# --------------------------------------------------------------------------


async def _tree(tmp_path):
    """The orchestrator's deps, an idle writer it spawned, and a read-only
    explorer it spawned (which holds the delegation tools at depth 1)."""
    deps = _deps(ScriptedProvider("ok"), mode=Mode.auto_edit, cwd=tmp_path)
    deps.pool = list(default_registry().tools.values())
    writer, _, _ = await spawn_subagent(deps, agent_type="general", prompt="write")
    reader, _, _ = await spawn_subagent(deps, agent_type="explore", prompt="read")
    reader_deps = deps.roster[reader].ctx.extra["subagent"]
    assert "write" in deps.roster[writer].registry.tools
    assert "write" not in deps.roster[reader].registry.tools
    return deps, writer, reader, reader_deps


async def test_a_subagent_cannot_drive_an_agent_it_did_not_spawn(tmp_path):
    """The roster is shared down the tree, so ``send_message`` could resume any
    id in it. A read-only explorer that had just read untrusted text could
    hand its instructions to an idle sibling that holds write and bash -- the
    permission boundary between the two, bypassed by one message."""
    deps, writer, _reader, reader_deps = await _tree(tmp_path)

    with pytest.raises(ValueError, match=f"'{writer}'.*not spawned") as refused:
        await resume_subagent(reader_deps, agent_id=writer, message="rm -rf src")
    assert deps.turns[writer] == 1
    assert writer not in str(refused.value).split("Known:")[-1]

    # Its own children are its to drive, and the orchestrator's are all its.
    mine, _, _ = await spawn_subagent(reader_deps, agent_type="explore", prompt="p")
    await resume_subagent(reader_deps, agent_id=mine, message="more")
    await resume_subagent(deps, agent_id=writer, message="more")
    await resume_subagent(deps, agent_id=mine, message="more")


async def test_a_subagent_sees_only_the_jobs_it_is_responsible_for(tmp_path):
    from quickcode.tools.agent_jobs import (
        AgentResultInput,
        AgentResultTool,
        AgentStatusInput,
        AgentStatusTool,
    )
    from quickcode.tools.base import ReadRegistry, ToolCtx

    deps, _writer, _reader, reader_deps = await _tree(tmp_path)
    owned: list[asyncio.Task] = []
    deps.adopt_task = reader_deps.adopt_task = owned.append
    theirs = spawn_subagent_background(deps, agent_type="explore", prompt="p")
    await asyncio.gather(*owned)

    ctx = ToolCtx(cwd=tmp_path, read_registry=ReadRegistry(),
                  extra={"subagent": reader_deps})
    listing = await AgentStatusTool().run(AgentStatusInput(), ctx)
    assert theirs.agent_id not in listing.content
    collected = await AgentResultTool().run(AgentResultInput(agent_id=theirs.agent_id), ctx)
    assert collected.is_error and not theirs.collected
