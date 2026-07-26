"""FleetView-style subagent list: compact selectable rows + one shared detail
panel, navigable with plain arrow keys entered from the chat input.

Covers: rows render for added agents, selection moves with focus_next/prev,
a finished agent revives back to live on a new live AgentStatus, close_finished
removes only finished rows, and pressing Down in an empty chat input hands
focus to the AgentPanes list."""

from pathlib import Path

from quickcode.app import ChatInput, QuickCodeApp
from quickcode.config import Config
from quickcode.core.agent import AgentInstance, EventBus
from quickcode.core.events import AgentStatus, TextDelta, ToolCallStart, TurnDone
from quickcode.core.history import History
from quickcode.core.permissions import Mode, PermissionEngine, Rules
from quickcode.tools.base import ReadRegistry, ToolCtx
from quickcode.tools.registry import default_registry
from quickcode.ui.agent_pane import AgentPane, AgentPanes


class _Prov:
    async def stream_chat(self, req):
        yield TextDelta("hi")
        yield TurnDone("stop")

    async def list_models(self):
        return []


def _agent() -> AgentInstance:
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


async def test_list_rows_render_for_added_agents():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        panes = app.query_one(AgentPanes)
        assert panes.display is False
        panes.add_pane("explore-1", "explore", EventBus())
        panes.add_pane("explore-2", "explore", EventBus())
        for _ in range(3):
            await pilot.pause()

        assert panes.display is True
        rows = list(panes.query(AgentPane))
        assert [r.agent_id for r in rows] == ["explore-1", "explore-2"]
        # Compact one-line row: live glyph + id + state, rendered markup=False.
        assert rows[0].content == "● explore-1  sending"
        assert rows[0].has_class("live")
        # First added row is selected by default.
        assert rows[0].has_class("selected")
        assert not rows[1].has_class("selected")


async def test_selection_moves_with_focus_next_prev():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        panes = app.query_one(AgentPanes)
        pane_a = panes.add_pane("general-1", "general", EventBus())
        pane_b = panes.add_pane("general-2", "general", EventBus())
        await pilot.pause()

        assert pane_a.has_class("selected")
        assert not pane_b.has_class("selected")

        panes.focus_next()
        await pilot.pause()
        assert not pane_a.has_class("selected")
        assert pane_b.has_class("selected")
        # Detail panel follows the selection.
        detail = panes.query_one("#panes-detail")
        assert "general-2" in detail.content

        panes.focus_prev()
        await pilot.pause()
        assert pane_a.has_class("selected")
        assert not pane_b.has_class("selected")


async def test_revival_flips_finished_back_to_live():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        panes = app.query_one(AgentPanes)
        bus = EventBus()
        pane = panes.add_pane("general-1", "general", bus)
        await pilot.pause()

        bus.emit(AgentStatus("idle"))
        for _ in range(6):
            await pilot.pause()
        assert pane.finished is True
        assert pane.has_class("finished")
        assert not pane.has_class("live")

        # send_message-style resume: a live status arrives after finishing.
        bus.emit(AgentStatus("streaming"))
        for _ in range(6):
            await pilot.pause()
        assert pane.finished is False
        assert pane.has_class("live")
        assert not pane.has_class("finished")
        assert pane.content.startswith("●")


async def test_close_finished_removes_only_finished():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        panes = app.query_one(AgentPanes)
        bus_a, bus_b = EventBus(), EventBus()
        panes.add_pane("general-1", "general", bus_a)
        panes.add_pane("general-2", "general", bus_b)
        await pilot.pause()

        bus_a.emit(ToolCallStart("t1", "grep"))
        bus_a.emit(AgentStatus("error", "boom"))
        for _ in range(6):
            await pilot.pause()

        panes.close_finished()
        await pilot.pause()
        remaining = list(panes.query(AgentPane))
        assert len(remaining) == 1
        assert remaining[0].agent_id == "general-2"
        assert panes.display is True


async def test_down_arrow_in_empty_chat_input_focuses_panes():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        chat = app.query_one("#chat-input", ChatInput)
        panes = app.query_one(AgentPanes)
        panes.add_pane("explore-1", "explore", EventBus())
        await pilot.pause()

        chat.text = ""
        chat.focus()
        await pilot.pause()
        assert app.focused is chat

        await pilot.press("down")
        await pilot.pause()
        assert app.focused is panes

        # Esc (app-level, priority) returns focus to the input.
        await pilot.press("escape")
        await pilot.pause()
        assert app.focused is chat


async def test_down_arrow_does_nothing_when_no_panes_or_text_present():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        chat = app.query_one("#chat-input", ChatInput)
        chat.focus()
        await pilot.pause()

        # No panes at all: Down should stay in the (empty) input.
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is chat

        # Panes exist, but the input has text: Down moves the cursor, not focus.
        panes = app.query_one(AgentPanes)
        panes.add_pane("explore-1", "explore", EventBus())
        chat.text = "hello"
        chat.move_cursor((0, 0))
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is chat


async def test_hint_line_changes_with_focus_and_enter_toggles_detail():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        panes = app.query_one(AgentPanes)
        panes.add_pane("explore-1", "explore", EventBus())
        await pilot.pause()

        hint = panes.query_one("#panes-hint")
        assert hint.content == "↓ to manage"

        panes.focus()
        for _ in range(3):
            await pilot.pause()
        assert "Esc back" in hint.content

        assert panes._detail_expanded is False
        await pilot.press("enter")
        await pilot.pause()
        assert panes._detail_expanded is True


async def test_mouse_selects_rows_and_button_toggles_detail():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        panes = app.query_one(AgentPanes)
        first = panes.add_pane("explore-1", "explore", EventBus())
        second = panes.add_pane("general-2", "general", EventBus())
        for _ in range(3):
            await pilot.pause()

        assert first.has_class("selected")
        await pilot.click(second, offset=(2, 0))
        await pilot.pause()
        assert second.has_class("selected")
        assert not first.has_class("selected")
        assert panes._detail_expanded is False

        await pilot.click("#panes-toggle")
        await pilot.pause()
        assert panes._detail_expanded is True
        assert panes.query_one("#panes-resize-handle") is not None


async def test_mouse_drag_resizes_subagent_pane():
    app = QuickCodeApp(_agent(), Config())
    async with app.run_test(size=(120, 40)) as pilot:
        panes = app.query_one(AgentPanes)
        panes.add_pane("explore-1", "explore", EventBus())
        for _ in range(3):
            await pilot.pause()

        handle = panes.query_one("#panes-resize-handle")
        original_width = panes.region.width
        target = (handle.region.x - 8, handle.region.y + 2)
        await pilot.mouse_down(handle)
        await pilot.hover(None, offset=target)
        await pilot.mouse_up(None, offset=target)
        await pilot.pause()

        assert panes.region.width > original_width
