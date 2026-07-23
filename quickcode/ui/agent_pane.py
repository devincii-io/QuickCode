"""Live subagent list: a FleetView-style compact, selectable list of running
and finished subagents plus one detail panel for whichever row is selected.

When the main agent spawns a subagent, ``AgentPanes.add_pane`` creates one
``AgentPane`` row that subscribes to the child's ``EventBus`` in ``__init__``
(so no events are missed between spawn and mount) and drains it on its own
interval, tracking state/text/tool-count. Each row renders as a single
compact line: a status glyph, the agent id, its state, and a running tool
count. The detail panel below the list always shows the currently selected
agent's status line, last tool, and the tail of its streaming text.

When the child emits ``AgentStatus`` idle/error/interrupted, the row marks
itself finished (dim, ○/✗). If a *live* ``AgentStatus`` (sending/streaming/
executing_tools) arrives afterwards — e.g. a finished subagent gets resumed
via ``send_message`` — the row revives back to live (●).

Navigation is keyboard-only: ``AgentPanes`` is focusable and binds its own
up/down/enter/x/escape so plain arrow keys drive the list once focused (no
leaking to app-level scroll bindings). The chat input hands off focus with a
plain Down press when it is empty (see ``ChatInput`` in ``app.py``). The
app's Ctrl+←/→/↑/↓/E/W bindings remain as aliases that call the same public
API: ``add_pane``/``focus_next``/``focus_prev``/``toggle_expand``/
``close_finished``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual import events
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static

from quickcode.core.events import (
    AgentStatus,
    TextDelta,
    ToolCallEnd,
    ToolCallStart,
)

if TYPE_CHECKING:
    from quickcode.core.agent import EventBus

# Detail-panel body height (lines of streaming text tail shown), compact vs
# expanded (Enter toggles).
_DETAIL_LINES = 12
_DETAIL_LINES_EXPANDED = 24

_FINISHED_STATES = {"idle", "error", "interrupted"}
_ERROR_STATES = {"error", "interrupted"}

_HINT_UNFOCUSED = "↓ to manage"
_HINT_FOCUSED = "↑/↓ select · Enter expand · x close done · Esc back"


def _tail(text: str, max_lines: int) -> str:
    """Last ``max_lines`` non-huge lines of the accumulated stream."""
    lines = text.replace("\r", "").split("\n")
    return "\n".join(lines[-max_lines:]).strip()


class AgentPane(Static):
    """One compact row in the subagent list. Subscribes to the child's bus in
    __init__ so no events are missed between spawn and mount; drains on its
    own interval and keeps enough state for the detail panel to render it."""

    DEFAULT_CSS = """
    AgentPane {
        height: 1;
        padding: 0 1;
        color: $text;
    }
    AgentPane.live {
        color: $success;
    }
    AgentPane.finished {
        color: $text-muted;
    }
    AgentPane.error {
        color: $error;
    }
    AgentPane.selected {
        background: $boost;
        border-left: outer $accent;
        text-style: bold;
    }
    """

    def __init__(self, agent_id: str, agent_type: str, bus: EventBus) -> None:
        super().__init__("", markup=False)
        self.agent_id = agent_id
        self.agent_type = agent_type
        self._queue = bus.subscribe(maxsize=0)
        self._state = "sending"
        self._text = ""
        self._tool_count = 0
        self._last_tool = ""
        self._finished = False
        self._expanded = False  # kept for API back-compat; unused for rows
        self._selected = False

    # ---- lifecycle ----

    def on_mount(self) -> None:
        self._refresh_row()
        # Poll the child's stream a bit slower than the main transcript — this
        # is a summary, not a full render.
        self.set_interval(1 / 15, self._drain)

    @property
    def finished(self) -> bool:
        return self._finished

    # ---- draining the child's bus ----

    def _drain(self) -> None:
        batch: list = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except Exception:
                break
        if not batch:
            return
        changed = False
        for ev in batch:
            if isinstance(ev, TextDelta):
                self._text += ev.text
                changed = True
            elif isinstance(ev, ToolCallStart):
                self._tool_count += 1
                self._last_tool = ev.name
                changed = True
            elif isinstance(ev, ToolCallEnd):
                if ev.name:
                    self._last_tool = ev.name
                changed = True
            elif isinstance(ev, AgentStatus):
                self._state = ev.state
                changed = True
                if ev.state in _FINISHED_STATES:
                    self._mark_finished()
                elif self._finished:
                    # A live status after finishing means the subagent was
                    # resumed (e.g. send_message) — revive it.
                    self._revive()
        if changed:
            self._refresh_row()

    def _mark_finished(self) -> None:
        if self._finished:
            return
        self._finished = True
        self._refresh_row()

    def _revive(self) -> None:
        if not self._finished:
            return
        self._finished = False
        self._refresh_row()

    # ---- selection (driven by AgentPanes) ----

    def set_selected(self, value: bool) -> None:
        self._selected = value
        try:
            self.set_class(value, "selected")
        except Exception:
            pass

    def set_expanded(self, value: bool) -> None:
        """Kept for API back-compat. The detail panel's own expansion is
        owned by ``AgentPanes`` since only one detail body is ever shown."""
        self._expanded = value

    # ---- rendering ----

    def _classify(self) -> tuple[str, str]:
        """(glyph, css class) for the current state."""
        if self._finished:
            if self._state in _ERROR_STATES:
                return "✗", "error"
            return "○", "finished"
        return "●", "live"

    def _refresh_row(self) -> None:
        # Note: don't gate this on self.is_mounted — that flag only flips
        # True in a `finally` block *after* on_mount returns, so it would
        # block the very first render (called from on_mount itself). Updating
        # our own content is safe any time we're in the tree; the try/except
        # only guards against a stray timer tick firing after removal.
        glyph, css_class = self._classify()
        for name in ("live", "finished", "error"):
            self.set_class(name == css_class, name)
        label = f"{glyph} {self.agent_id}  {self._state}"
        if self._tool_count:
            label += f" · {self._tool_count} tool" + ("s" if self._tool_count != 1 else "")
        try:
            self.update(label)
        except Exception:
            pass

    def render_detail(self, max_lines: int) -> str:
        """Full detail text for the (single, shared) detail panel."""
        glyph, _ = self._classify()
        lines = [f"{glyph} {self.agent_id} ({self.agent_type}) · {self._state}"]
        if self._tool_count:
            tool_label = f"{self._tool_count} tool" + ("s" if self._tool_count != 1 else "")
            if self._last_tool:
                tool_label += f" · last: {self._last_tool}"
            lines.append(tool_label)
        tail = _tail(self._text, max_lines)
        if tail:
            lines.append("")
            lines.append(tail)
        return "\n".join(lines)


class AgentPanes(Vertical):
    """A keyboard-navigable, focusable list of ``AgentPane`` rows plus one
    detail panel for the selected row. Hidden while empty.

    A small always-visible header shows the agent count; the hint line is
    context-sensitive (unfocused vs focused) so the controls are discoverable
    without opening help.
    """

    can_focus = True

    BINDINGS = [
        Binding("up", "cursor_up", "Select prev", show=False),
        Binding("down", "cursor_down", "Select next", show=False),
        Binding("enter", "toggle_expand", "Expand", show=False),
        Binding("x", "close_finished", "Close done", show=False),
        Binding("escape", "focus_input", "Back", show=False),
    ]

    DEFAULT_CSS = """
    AgentPanes {
        width: 42;
        height: 1fr;
        background: $panel;
        border-left: vkey $primary-darken-2;
        padding: 1 1;
        overflow-y: auto;
    }
    AgentPanes:focus {
        border-left: vkey $accent;
    }
    AgentPanes #panes-header {
        color: $accent;
        text-style: bold;
        height: 1;
    }
    AgentPanes #panes-hint {
        color: $text-muted;
        height: auto;
        margin: 0 0 1 0;
    }
    AgentPanes #panes-list {
        height: auto;
    }
    AgentPanes #panes-detail {
        color: $text-muted;
        height: auto;
        margin: 1 0 0 0;
        border-top: solid $panel-lighten-2;
        padding: 1 0 0 0;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._panes: list[AgentPane] = []
        self._selected = -1
        self._detail_expanded = False

    def compose(self):
        yield Static("Subagents", id="panes-header", markup=False)
        yield Static(_HINT_UNFOCUSED, id="panes-hint", markup=False)
        yield Vertical(id="panes-list")
        yield Static("", id="panes-detail", markup=False)

    def on_mount(self) -> None:
        self._update_visibility()
        self._update_header()
        self._refresh_detail()
        # Keep the live/done count fresh as panes finish on their own timers.
        self.set_interval(0.5, self._update_header)
        # Keep the detail panel's streaming tail fresh for the selected row.
        self.set_interval(1 / 15, self._refresh_detail)

    def on_focus(self, event: events.Focus) -> None:
        # Handlers dispatch most-derived-class-first, so Widget._on_focus
        # (which flips the has_focus reactive) hasn't run yet at this point —
        # key off the event itself rather than self.has_focus.
        self._update_hint(focused=True)

    def on_blur(self, event: events.Blur) -> None:
        self._update_hint(focused=False)

    def _update_hint(self, *, focused: bool) -> None:
        try:
            hint = self.query_one("#panes-hint", Static)
        except Exception:
            return
        hint.update(_HINT_FOCUSED if focused else _HINT_UNFOCUSED)

    # ---- public API used by the app ----

    def add_pane(self, agent_id: str, agent_type: str, bus: EventBus) -> AgentPane:
        pane = AgentPane(agent_id, agent_type, bus)
        self._panes.append(pane)
        try:
            self.query_one("#panes-list", Vertical).mount(pane)
        except Exception:
            pass
        if self._selected < 0:
            self._selected = 0
        self._sync_selection()
        self._update_visibility()
        self._update_header()
        self._refresh_detail()
        return pane

    def _update_header(self) -> None:
        try:
            header = self.query_one("#panes-header", Static)
        except Exception:
            return
        n = len(self._panes)
        live = sum(1 for p in self._panes if not p.finished)
        label = f"Subagents · {n}"
        if live:
            label += f"  ({live} live)"
        elif n:
            label += "  (all done)"
        header.update(label)

    def focus_next(self) -> None:
        self._move(1)

    def focus_prev(self) -> None:
        self._move(-1)

    def toggle_expand(self) -> None:
        """Toggle the shared detail panel between compact and tall."""
        self._detail_expanded = not self._detail_expanded
        self._refresh_detail()

    def close_finished(self) -> None:
        """Remove every finished row, keeping the live ones."""
        remaining: list[AgentPane] = []
        for pane in self._panes:
            if pane.finished:
                try:
                    pane.remove()
                except Exception:
                    pass
            else:
                remaining.append(pane)
        self._panes = remaining
        if self._selected >= len(self._panes):
            self._selected = len(self._panes) - 1
        self._sync_selection()
        self._update_visibility()
        self._update_header()
        self._refresh_detail()

    # ---- keyboard actions (bound above) ----

    def action_cursor_up(self) -> None:
        self.focus_prev()

    def action_cursor_down(self) -> None:
        self.focus_next()

    def action_toggle_expand(self) -> None:
        self.toggle_expand()

    def action_close_finished(self) -> None:
        self.close_finished()

    def action_focus_input(self) -> None:
        try:
            self.app.query_one("#chat-input").focus()
        except Exception:
            pass

    # ---- internals ----

    def _move(self, delta: int) -> None:
        if not self._panes:
            return
        if self._selected < 0:
            self._selected = 0
        else:
            self._selected = (self._selected + delta) % len(self._panes)
        self._sync_selection()
        try:
            self._panes[self._selected].scroll_visible(animate=False)
        except Exception:
            pass
        self._refresh_detail()

    def _sync_selection(self) -> None:
        for i, pane in enumerate(self._panes):
            pane.set_selected(i == self._selected)

    def _update_visibility(self) -> None:
        self.display = bool(self._panes)

    def _refresh_detail(self) -> None:
        try:
            detail = self.query_one("#panes-detail", Static)
        except Exception:
            return
        if not (0 <= self._selected < len(self._panes)):
            detail.update("")
            return
        pane = self._panes[self._selected]
        max_lines = _DETAIL_LINES_EXPANDED if self._detail_expanded else _DETAIL_LINES
        detail.update(pane.render_detail(max_lines))
