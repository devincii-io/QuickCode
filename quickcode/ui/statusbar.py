"""Status bar: structured, color-coded segments across the footer.

model · context% · session cost · permission mode (color-coded) · agent state.
Rendered as separate Static segments so each can carry its own background,
rather than one flat markup line.
"""

from __future__ import annotations

from textual.containers import Horizontal
from textual.reactive import reactive
from textual.widgets import Static

from quickcode.core.permissions import Mode

_MODE_LABEL = {
    Mode.plan: "PLAN",
    Mode.ask: "ASK",
    Mode.auto_edit: "AUTO-EDIT",
    Mode.dontask: "DONTASK",
    Mode.yolo: "YOLO",
}

_MODE_CLASS = {
    Mode.plan: "mode-plan",
    Mode.ask: "mode-ask",
    Mode.auto_edit: "mode-auto-edit",
    Mode.dontask: "mode-ask",
    Mode.yolo: "mode-yolo",
}

_STATE_GLYPH = {
    "idle": "●",
    "sending": "◐",
    "streaming": "◑",
    "executing_tools": "◒",
    "interrupted": "✗",
    "error": "⚠",
}


class StatusBar(Horizontal):
    """A single-line footer of colored segments showing live agent state."""

    model: reactive[str] = reactive("")
    ctx_pct: reactive[float | None] = reactive(None)
    cost_usd: reactive[float] = reactive(0.0)
    mode: reactive[Mode] = reactive(Mode.ask)
    agent_state: reactive[str] = reactive("idle")

    def compose(self):
        yield Static("", id="seg-model", markup=False)
        yield Static("", id="seg-ctx", markup=False)
        yield Static("", id="seg-cost", markup=False)
        yield Static("", id="seg-mode", markup=False)
        yield Static("", id="seg-state", markup=False)

    def on_mount(self) -> None:
        self._refresh()

    def _seg(self, sel: str) -> Static | None:
        try:
            return self.query_one(sel, Static)
        except Exception:
            return None

    def _refresh(self) -> None:
        if not self.is_mounted:
            return
        if (s := self._seg("#seg-model")) is not None:
            s.update(f" {self.model or '(no model)'} ")
        if (s := self._seg("#seg-ctx")) is not None:
            ctx = f"{self.ctx_pct:.0f}%" if self.ctx_pct is not None else "--"
            s.update(f" ctx {ctx} ")
        if (s := self._seg("#seg-cost")) is not None:
            s.update(f" ${self.cost_usd:.4f} ")
        if (s := self._seg("#seg-mode")) is not None:
            for cls in _MODE_CLASS.values():
                s.remove_class(cls)
            s.add_class(_MODE_CLASS.get(self.mode, "mode-ask"))
            s.update(f" {_MODE_LABEL.get(self.mode, str(self.mode))} ")
        if (s := self._seg("#seg-state")) is not None:
            glyph = _STATE_GLYPH.get(self.agent_state, "●")
            s.update(f" {glyph} {self.agent_state} ")

    # reactive watchers -> re-render the affected segments
    def watch_model(self, _v: str) -> None:
        self._refresh()

    def watch_ctx_pct(self, _v: float | None) -> None:
        self._refresh()

    def watch_cost_usd(self, _v: float) -> None:
        self._refresh()

    def watch_mode(self, _v: Mode) -> None:
        self._refresh()

    def watch_agent_state(self, _v: str) -> None:
        self._refresh()

    def update_usage(self, ctx_pct: float | None, cost_usd: float) -> None:
        self.ctx_pct = ctx_pct
        self.cost_usd = cost_usd
