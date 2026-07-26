"""Keyboard-only focus flow: Tab into the transcript, toggle a collapsible with
Enter, Esc back to the input, and Shift+Tab overloaded (mode vs focus-back)."""

from pathlib import Path

from textual.widgets import Collapsible
from textual.widgets._collapsible import CollapsibleTitle

from quickcode.app import ChatInput, QuickCodeApp
from quickcode.config import Config
from quickcode.core.agent import AgentInstance
from quickcode.core.events import ReasoningDelta, TextDelta, TurnDone
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry
from quickcode.ui.transcript import Transcript


class _Prov:
    async def stream_chat(self, req):
        yield ReasoningDelta("thinking ")
        for w in ("the ", "answer."):
            yield TextDelta(w)
        yield TurnDone("stop")

    async def list_models(self):
        return []


def _agent():
    ctx = ToolCtx(cwd=Path.cwd(), read_registry=ReadRegistry(), extra={})
    return AgentInstance(
        name="m",
        provider=_Prov(),
        registry=default_registry(),
        history=History("SYS"),
        ctx=ctx,
        permissions=PermissionEngine(Mode.ask, Rules(), Path.cwd()),
        model="t",
        permission_cb=None,
    )


async def test_keyboard_flows_between_input_and_thinking():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(100, 40)) as pilot:
        chat = app.query_one("#chat-input", ChatInput)
        chat.focus()
        transcript = app.query_one(Transcript)
        transcript.add_user("q")
        await app._run_turn("q")
        app._drain_bus()
        for _ in range(10):
            await pilot.pause()

        box = next(b for b in transcript.query(Collapsible) if "reasoning-box" in b.classes)

        # Tab out of the input until a collapsible title has focus.
        title = None
        for _ in range(6):
            await pilot.press("tab")
            await pilot.pause()
            if isinstance(app.focused, CollapsibleTitle):
                title = app.focused
                break
        assert title is not None
        assert app.focused is not chat

        # Enter toggles the thinking block.
        was = box.collapsed
        await pilot.press("enter")
        await pilot.pause()
        assert box.collapsed is not was

        # Esc returns focus to the input.
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is chat

        # Shift+Tab in the input cycles mode (does not move focus).
        before = app.agent.mode
        await pilot.press("shift+tab")
        await pilot.pause()
        assert app.agent.mode != before
        assert app.focused is chat


async def test_transcript_click_toggles_block_then_restores_input_focus():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(100, 40)) as pilot:
        chat = app.query_one("#chat-input", ChatInput)
        transcript = app.query_one(Transcript)
        transcript.tool_result("tool-1", "read", "result body", False)
        await pilot.pause()

        box = next(iter(transcript.query(Collapsible)))
        title = box.query_one(CollapsibleTitle)
        was = box.collapsed

        await pilot.click(title)
        await pilot.pause()

        assert box.collapsed is not was
        assert app.focused is chat


async def test_down_from_transcript_control_returns_to_input():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(100, 40)) as pilot:
        chat = app.query_one("#chat-input", ChatInput)
        transcript = app.query_one(Transcript)
        transcript.tool_result("tool-1", "read", "result body", False)
        await pilot.pause()

        title = next(iter(transcript.query(Collapsible))).query_one(CollapsibleTitle)
        title.focus()
        await pilot.pause()
        assert app.focused is title

        await pilot.press("down")
        await pilot.pause()

        assert app.focused is chat
