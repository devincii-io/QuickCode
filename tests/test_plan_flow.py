import json
from collections.abc import AsyncIterator
from pathlib import Path

from quickcode.core.agent import AgentInstance, PermissionOutcome, PlanOutcome
from quickcode.core.events import (
    AssembledToolCall,
    TextDelta,
    TurnDone,
)
from quickcode.core.history import History
from quickcode.core.loop import _tools_for
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry


class OneCallProvider:
    """Emits a single tool call on the first request, then plain text."""

    def __init__(self, call: AssembledToolCall) -> None:
        self.call = call
        self._sent = False

    async def stream_chat(self, req) -> AsyncIterator:
        if not self._sent:
            self._sent = True
            from quickcode.core.events import ToolCallEnd, ToolCallStart

            yield ToolCallStart(self.call.id, self.call.name)
            yield ToolCallEnd(self.call.id, self.call.name, self.call.arguments)
            yield TurnDone("tool_calls")
        else:
            yield TextDelta("done")
            yield TurnDone("stop")

    async def list_models(self):
        return []


async def _deny(_r):
    return PermissionOutcome(allow=False)


def _agent(provider, mode=Mode.plan):
    ctx = ToolCtx(cwd=Path.cwd(), read_registry=ReadRegistry(), extra={})
    return AgentInstance(
        name="main",
        provider=provider,
        registry=default_registry(),
        history=History("SYS"),
        ctx=ctx,
        permissions=PermissionEngine(mode, Rules(), Path.cwd()),
        model="test/model",
        permission_cb=_deny,
    )


def test_plan_mode_withholds_mutation_and_offers_plan():
    agent = _agent(OneCallProvider(AssembledToolCall("1", "plan", "{}")), mode=Mode.plan)
    names = {s.name for s in _tools_for(agent)}
    assert "plan" in names
    assert "write" not in names and "edit" not in names
    assert "read" in names and "bash" in names  # read-only tools remain


def test_non_plan_mode_hides_plan_tool():
    agent = _agent(OneCallProvider(AssembledToolCall("1", "plan", "{}")), mode=Mode.ask)
    names = {s.name for s in _tools_for(agent)}
    assert "plan" not in names
    assert "write" in names and "edit" in names


async def test_plan_approval_switches_mode():
    call = AssembledToolCall("1", "plan", json.dumps({"plan": "# do things"}))
    agent = _agent(OneCallProvider(call), mode=Mode.plan)

    async def approve(_md):
        return PlanOutcome(approved=True, mode_after=Mode.auto_edit)

    agent.plan_cb = approve
    await agent.run_turn("make a plan")
    assert agent.mode == Mode.auto_edit
    assert agent.approved_plan == "# do things"


async def test_plan_rejection_keeps_planning():
    call = AssembledToolCall("1", "plan", json.dumps({"plan": "# do things"}))
    agent = _agent(OneCallProvider(call), mode=Mode.plan)

    async def reject(_md):
        return PlanOutcome(approved=False, feedback="add tests")

    agent.plan_cb = reject
    await agent.run_turn("make a plan")
    assert agent.mode == Mode.plan  # unchanged
    tool_msgs = [m for m in agent.history.messages if m.role == "tool"]
    assert any("add tests" in m.content for m in tool_msgs)
