"""Live subagent panes.

When the main agent spawns a subagent, ``AgentPanes.add_pane`` mounts one
``AgentPane`` that subscribes to the child's ``EventBus`` and drains it on its
own interval, rendering a COMPACT summary of the child's activity: a title row
with a status glyph, the tail of its streaming text, and a small running tool
count. When the child emits ``AgentStatus`` idle/error/interrupted the pane
marks itself finished (dim, ✓/✗) and auto-collapses to the title row.

Navigation is keyboard-only and driven by the app: ``AgentPanes`` keeps an
internal selected index with a visible highlight; the app's bindings call
``focus_next``/``focus_prev``/``toggle_expand``/``close_finished``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

# Compact vs expanded body height (lines of streaming text shown).
_COMPACT_LINES = 3
_EXPANDED_LINES = 12

_FINISHED_STATES = {"idle", "error", "interrupted"}

_GLYPH = {
    "sending": "…",
    "streaming": "▶",
    "executing_tools": "⚙",
    "idle": "✓",
    "error": "✗",
    "interrupted": "⊘",
}


def _tail(text: str, max_lines: int) -> str:
    """Last ``max_lines`` non-huge lines of the accumulated stream."""
    lines = text.replace("\r", "").split("\n")
    return "\n".join(lines[-max_lines:]).strip()


class AgentPane(Vertical):
    """One live subagent summary. Subscribes to the child's bus in __init__ so
    no events are missed between spawn and mount; drains on its own interval."""

    DEFAULT_CSS = """
    AgentPane {
        height: auto;
        border-left: outer $panel-lighten-2;
        padding: 0 1;
        margin: 0 0 1 0;
    }
    AgentPane.selected {
        border-left: outer $accent;
    }
    AgentPane.finished {
        color: $text-muted;
    }
    AgentPane .pane-title {
        text-style: bold;
        color: $secondary;
    }
    AgentPane.finished .pane-title {
        color: $text-muted;
        text-style: none;
    }
    AgentPane .pane-body {
        color: $text-muted;
        height: auto;
    }
    """

    def __init__(self, agent_id: str, agent_type: str, bus: EventBus) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.agent_type = agent_type
        self._queue = bus.subscribe(maxsize=0)
        self._state = "sending"
        self._text = ""
        self._tool_count = 0
        self._last_tool = ""
        self._finished = False
        self._expanded = False
        self._selected = False

    # ---- lifecycle ----

    def compose(self):
        yield Static("", classes="pane-title", markup=False)
        yield Static("", classes="pane-body", markup=False)

    def on_mount(self) -> None:
        self._refresh_view()
        # Poll the child's stream a bit slower than the main transcript — this is
        # a summary, not a full render.
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
        if changed:
            self._refresh_view()

    def _mark_finished(self) -> None:
        if self._finished:
            return
        self._finished = True
        try:
            self.add_class("finished")
        except Exception:
            pass
        self._apply_body_visibility()

    # ---- selection / expansion (driven by AgentPanes) ----

    def set_selected(self, value: bool) -> None:
        self._selected = value
        try:
            self.set_class(value, "selected")
        except Exception:
            pass

    def set_expanded(self, value: bool) -> None:
        self._expanded = value
        try:
            self.set_class(value, "expanded")
        except Exception:
            pass
        self._apply_body_visibility()
        self._refresh_view()

    def _apply_body_visibility(self) -> None:
        """Finished panes collapse to the title row unless expanded."""
        try:
            body = self.query_one(".pane-body", Static)
        except Exception:
            return
        body.display = self._expanded or not self._finished

    # ---- rendering ----

    def _refresh_view(self) -> None:
        if not self.is_mounted:
            return
        glyph = _GLYPH.get(self._state, "·")
        try:
            title = self.query_one(".pane-title", Static)
            title.update(f"▍ {self.agent_id} · {glyph} {self._state}")
        except Exception:
            return
        max_lines = _EXPANDED_LINES if self._expanded else _COMPACT_LINES
        lines: list[str] = []
        if self._tool_count:
            label = f"{self._tool_count} tool" + ("s" if self._tool_count != 1 else "")
            if self._last_tool:
                label += f" · {self._last_tool}"
            lines.append(label)
        tail = _tail(self._text, max_lines)
        if tail:
            lines.append(tail)
        try:
            body = self.query_one(".pane-body", Static)
            body.update("\n".join(lines))
            self._apply_body_visibility()
        except Exception:
            pass


class AgentPanes(Vertical):
    """A keyboard-navigable stack of ``AgentPane`` rows. Hidden while empty.

    A small always-visible header shows the agent count and the control hints,
    so the panes are discoverable and dismissable without opening help.
    """

    DEFAULT_CSS = """
    AgentPanes {
        width: 42;
        height: 1fr;
        background: $panel;
        border-left: vkey $primary-darken-2;
        padding: 1 1;
        overflow-y: auto;
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
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._panes: list[AgentPane] = []
        self._selected = -1

    def compose(self):
        yield Static("Subagents", id="panes-header", markup=False)
        yield Static(
            "Ctrl+←/→ move · Ctrl+E expand · Ctrl+W close done",
            id="panes-hint",
            markup=False,
        )

    def on_mount(self) -> None:
        self._update_visibility()
        # Keep the live/done count fresh as panes finish on their own timers.
        self.set_interval(0.5, self._update_header)

    # ---- public API used by the app ----

    def add_pane(self, agent_id: str, agent_type: str, bus: EventBus) -> AgentPane:
        pane = AgentPane(agent_id, agent_type, bus)
        self._panes.append(pane)
        try:
            self.mount(pane)
        except Exception:
            pass
        if self._selected < 0:
            self._selected = 0
        self._sync_selection()
        self._update_visibility()
        self._update_header()
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
        """Expand the selected pane and collapse the rest."""
        if not (0 <= self._selected < len(self._panes)):
            return
        target = self._panes[self._selected]
        want = not target._expanded
        for i, pane in enumerate(self._panes):
            pane.set_expanded(want and i == self._selected)

    def close_finished(self) -> None:
        """Remove every finished pane, keeping the live ones."""
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

    def _sync_selection(self) -> None:
        for i, pane in enumerate(self._panes):
            pane.set_selected(i == self._selected)

    def _update_visibility(self) -> None:
        self.display = bool(self._panes)
