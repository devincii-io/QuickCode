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
    cwd: reactive[str] = reactive("")
    ctx_pct: reactive[float | None] = reactive(None)
    cost_usd: reactive[float] = reactive(0.0)
    mode: reactive[Mode] = reactive(Mode.ask)
    agent_state: reactive[str] = reactive("idle")

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Cached segment refs (filled in on_mount) so watchers don't re-query the
        # DOM, and last-rendered strings so we skip no-op repaints.
        self._segs: dict[str, Static] = {}
        self._last: dict[str, str] = {}

    def compose(self):
        yield Static("", id="seg-model", markup=False)
        yield Static("", id="seg-cwd", markup=False)
        yield Static("", id="seg-ctx", markup=False)
        yield Static("", id="seg-cost", markup=False)
        yield Static("", id="seg-mode", markup=False)
        yield Static("", id="seg-state", markup=False)

    def on_mount(self) -> None:
        for key in ("model", "cwd", "ctx", "cost", "mode", "state"):
            try:
                self._segs[key] = self.query_one(f"#seg-{key}", Static)
            except Exception:
                pass
        self._set("model", f" {self.model or '(no model)'} ")
        self._set("cwd", f" {self.cwd or '(no cwd)'} ")
        self._set("ctx", self._ctx_text())
        self._set("cost", f" ${self.cost_usd:.4f} ")
        self._set_mode()
        self._set("state", self._state_text())

    def _set(self, key: str, text: str) -> None:
        seg = self._segs.get(key)
        if seg is None or self._last.get(key) == text:
            return  # skip: not mounted, or the rendered string is unchanged
        self._last[key] = text
        seg.update(text)

    def _ctx_text(self) -> str:
        if self.ctx_pct is None:
            ctx = "--"
        elif 0 < self.ctx_pct < 1:
            ctx = "<1%"
        else:
            ctx = f"{self.ctx_pct:.0f}%"
        return f" ctx {ctx} "

    def _state_text(self) -> str:
        return f" {_STATE_GLYPH.get(self.agent_state, '●')} {self.agent_state} "

    def _set_mode(self) -> None:
        seg = self._segs.get("mode")
        if seg is None:
            return
        for cls in _MODE_CLASS.values():
            seg.remove_class(cls)
        seg.add_class(_MODE_CLASS.get(self.mode, "mode-ask"))
        self._set("mode", f" {_MODE_LABEL.get(self.mode, str(self.mode))} ")

    # reactive watchers -> update only the affected segment
    def watch_model(self, _v: str) -> None:
        self._set("model", f" {self.model or '(no model)'} ")

    def watch_cwd(self, _v: str) -> None:
        self._set("cwd", f" {self.cwd or '(no cwd)'} ")

    def watch_ctx_pct(self, _v: float | None) -> None:
        self._set("ctx", self._ctx_text())

    def watch_cost_usd(self, _v: float) -> None:
        self._set("cost", f" ${self.cost_usd:.4f} ")

    def watch_mode(self, _v: Mode) -> None:
        self._set_mode()

    def watch_agent_state(self, _v: str) -> None:
        self._set("state", self._state_text())

    def update_usage(self, ctx_pct: float | None, cost_usd: float) -> None:
        self.ctx_pct = ctx_pct
        self.cost_usd = cost_usd
