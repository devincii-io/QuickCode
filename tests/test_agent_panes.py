"""Live subagent panes: spawning surfaces a pane that streams the child's
activity and finishes on AgentStatus idle; keyboard navigation + dismissal;
and the container hides itself when empty."""

import json
from collections.abc import AsyncIterator
from pathlib import Path

from quickcode.app import QuickCodeApp
from quickcode.config import Config, Environment, Profile
from quickcode.core.agent import AgentInstance, EventBus
from quickcode.core.events import (
    AgentStatus,
    AssembledToolCall,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
    TurnDone,
)
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.subagents.runner import SubagentDeps
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry
from quickcode.ui.agent_pane import AgentPane, AgentPanes


class _ChildProvider:
    """The subagent's provider: streams a couple text deltas, then finishes.
    (The loop itself emits AgentStatus idle at the very end.)"""

    async def stream_chat(self, req) -> AsyncIterator:
        yield TextDelta("scanning the repo... ")
        yield TextDelta("found it in core/loop.py")
        yield TurnDone("stop")

    async def list_models(self):
        return []


class _MainProvider:
    """Main agent: spawns one subagent via the `agent` tool, then wraps up."""

    def __init__(self, call: AssembledToolCall) -> None:
        self.call = call
        self._sent = False

    async def stream_chat(self, req) -> AsyncIterator:
        if not self._sent:
            self._sent = True
            yield ToolCallStart(self.call.id, self.call.name)
            yield ToolCallEnd(self.call.id, self.call.name, self.call.arguments)
            yield TurnDone("tool_calls")
        else:
            yield TextDelta("delegated and done")
            yield TurnDone("stop")


def _spawn_agent() -> AgentInstance:
    cwd = Path.cwd()
    child_provider = _ChildProvider()
    deps = SubagentDeps(
        provider=child_provider,
        profile=Profile(),
        env=Environment.detect(cwd),
        mode_getter=lambda: Mode.auto_edit,
        cwd=cwd,
    )
    ctx = ToolCtx(cwd=cwd, read_registry=ReadRegistry(), extra={"subagent": deps})
    call = AssembledToolCall(
        "t1",
        "agent",
        json.dumps({"agent_type": "general", "prompt": "find the bug", "description": "hunt bug"}),
    )
    return AgentInstance(
        name="main",
        provider=_MainProvider(call),
        registry=default_registry(),
        history=History("SYS"),
        ctx=ctx,
        # auto-edit so the (mutating) agent tool auto-allows without a modal.
        permissions=PermissionEngine(Mode.auto_edit, Rules(), cwd),
        model="test/model",
        permission_cb=None,
    )


async def test_spawn_creates_streaming_pane_that_finishes():
    app = QuickCodeApp(_spawn_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        await app._run_turn("go find the bug")
        # Let the pane mount and drain its queue over a few interval ticks.
        for _ in range(10):
            await pilot.pause()
        panes = app.query_one(AgentPanes)
        rows = list(panes.query(AgentPane))
        assert len(rows) == 1
        pane = rows[0]
        assert pane.agent_id == "general-1"
        assert "found it in core/loop.py" in pane._text
        assert pane.finished is True


async def test_panes_hidden_when_empty_shown_after_add():
    app = QuickCodeApp(_spawn_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        panes = app.query_one(AgentPanes)
        assert panes.display is False
        panes.add_pane("explore-1", "explore", EventBus())
        await pilot.pause()
        assert panes.display is True


async def test_navigation_and_close_finished():
    app = QuickCodeApp(_spawn_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        panes = app.query_one(AgentPanes)
        bus_a, bus_b = EventBus(), EventBus()
        pane_a = panes.add_pane("general-1", "general", bus_a)
        pane_b = panes.add_pane("general-2", "general", bus_b)
        await pilot.pause()

        # First pane selected by default.
        assert pane_a._selected is True and pane_b._selected is False

        app.action_pane_next()
        assert pane_a._selected is False and pane_b._selected is True
        app.action_pane_prev()
        assert pane_a._selected is True and pane_b._selected is False

        # Finish pane_a via a status event, then dismiss finished panes.
        bus_a.emit(AgentStatus("idle"))
        for _ in range(6):
            await pilot.pause()
        assert pane_a.finished is True

        app.action_pane_close_finished()
        await pilot.pause()
        remaining = list(panes.query(AgentPane))
        assert len(remaining) == 1
        assert remaining[0].agent_id == "general-2"
