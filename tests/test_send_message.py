"""send_message: resuming a finished subagent with its context intact instead
of respawning a fresh one."""

from pathlib import Path

import pytest

from quickcode.config import Environment, Profile
from quickcode.core.events import TextDelta, TurnDone
from quickcode.core.permissions import Mode
from quickcode.subagents.runner import (
    SubagentDeps,
    resume_subagent,
    spawn_subagent,
)
from quickcode.tools.agent import AgentTool
from quickcode.tools.registry import build_registry, default_registry
from quickcode.tools.send_message import SendMessageTool


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
    agent_id, _ = await spawn_subagent(deps, agent_type="explore", prompt="find it")
    assert agent_id in deps.roster
    assert deps.roster[agent_id].name == agent_id


async def test_resume_returns_sanitized_report_and_keeps_history():
    deps = _deps(ScriptedProvider("first reply", "second reply"))
    agent_id, first_report = await spawn_subagent(
        deps, agent_type="general", prompt="do the first thing"
    )
    assert "first reply" in first_report

    resumed_id, second_report = await resume_subagent(
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
    a1, _ = await spawn_subagent(deps, agent_type="explore", prompt="p1")
    a2, _ = await spawn_subagent(deps, agent_type="general", prompt="p2")

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
    agent_id, _ = await spawn_subagent(deps, agent_type="general", prompt="p")
    child = deps.roster[agent_id]
    child.busy = True  # simulate an in-flight turn
    with pytest.raises(ValueError, match=f"agent '{agent_id}' is still running"):
        await resume_subagent(deps, agent_id=agent_id, message="hi")


async def test_send_message_tool_round_trips_through_deps():
    from quickcode.tools.base import ReadRegistry, ToolCtx

    deps = _deps(ScriptedProvider("first reply", "second reply"))
    agent_id, _ = await spawn_subagent(deps, agent_type="general", prompt="p1")

    ctx = ToolCtx(cwd=Path.cwd(), read_registry=ReadRegistry(), extra={"subagent": deps})
    tool = SendMessageTool()
    result = await tool.run(SendMessageTool.Input(agent_id=agent_id, message="p2"), ctx)
    assert not result.is_error
    assert f'<subagent id="{agent_id}">' in result.content
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
